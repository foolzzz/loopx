# Dispatcher (S4)

Implements design decisions 2-4 and 19 of [design-v0](design-v0.md). It is also where decision 17 (auth preflight) and decision 18 (per-todo workspaces) take effect at launch time. Code
lives in `loopx/dispatch/`; the CLI is `loopx dispatch`.

The dispatcher is a scheduler. It is not a second state machine. For each goal and
agent it asks LoopX whether the agent should run now (the quota `should-run`
decision, called through the Python API). It then applies slot, cooldown and auth
rules, and launches `loopx turn run-once` as a child process. The Turn pipeline owns
the typed result, validation, idempotent writeback and quota spend. The dispatcher
never writes goal state directly. Its only goal writes are user gates (re-login,
long cooldown, orchestrator repeat limit, push request and budget exhausted),
which it creates through the normal Todo API (`add_goal_todo`, the same path as
`todo add --role user --task-class user_gate`).

## Running it

```sh
# one reconcile pass; waits for the Turns it launched (cron / tests)
loopx dispatch serve --goal-id G --once

# resident: watch state files, reconcile on changes and every tick
loopx dispatch serve --goal-id G [--goal-id G2 ...] [--project P] \
  [--tick-seconds 60] [--poll-seconds 3] [--max-global 4] \
  [--turn-timeout-seconds 3600] [--long-cooldown-seconds 3600] \
  [--backoff-base-seconds 60] [--validation-command-json '["make","test"]']

loopx dispatch status            # running Turns, per-agent slots, cooldowns, gates
loopx dispatch launchd-plist --goal-id G > ~/Library/LaunchAgents/com.loopx.dispatch.plist
launchctl load ~/Library/LaunchAgents/com.loopx.dispatch.plist   # you install it; the command only prints
```

The global `--registry` and `--runtime-root` options select the state home as usual.
Only one dispatcher can run per runtime root. `serve` and `serve --once` take an
exclusive `flock` on `<runtime-root>/dispatch/serve.lock`. A second dispatcher on the
same runtime root exits with code 3 (`dispatcher_locked`).

The plist starts the dispatcher with `KeepAlive` and `RunAtLoad`, and uses the Python
interpreter that rendered it. It copies the current `PATH`, so `claude`, `codex` and
`git` resolve the same way they do in your shell. Its logs go to
`<runtime-root>/dispatch/logs/`.

## What one reconcile pass does

1. **Reap** finished children. The pass classifies each child from its exit code and
   its `--format json` output: `committed`, `host_failed` (with the host's
   `failure_kind`), `failed` or `crashed`.
2. For each goal, load the registered agents and their registry roles (S1), and
   resolve each agent's config file (S3 `resolve_agent`). The pass skips disabled
   agents and agents whose config is invalid, and reports why.
3. **Slots.** An orchestrator is limited to 1 per goal, whatever its
   `max_concurrency` says. Developers and acceptors run up to their
   `max_concurrency`, counted per agent across goals, and under role_v1 that
   includes several todos of the same goal (decision 32). Every launch counts
   against `--max-global`.
4. **Ask LoopX.** The pass calls `collect_status` and then `build_quota_should_run`
   for that agent:
   - `should_run=false`: the agent is idle.
   - A selected todo in the agent's lane (role-aware selection from S1): launch.
   - Nobody launches without a selected todo, the orchestrator included:
     `turn run-once` refuses every host route without todo lineage, so a
     todo-less Turn could only fail (the E2E pilot saw a relaunch hot loop). A
     pending orchestrator action is reported as `orchestrator_action_without_todo`.
     The orchestrator is event-triggered through its todos: the intake planning
     todo, S2 escalation todos, and gate threads awaiting it (a gate whose thread
     awaits the orchestrator no longer blocks its lane, see
     [gates-plans-intake-v0](gates-plans-intake-v0.md)).
   - When the orchestrator does not launch but has work no todo carries (an
     `orchestrator_action_without_todo`, or open gates whose threads await it),
     the dispatcher opens one orchestrator todo ("Orchestrator action: …",
     `action_kind=replan`, `required_role=orchestrator`, no acceptance) through
     the Todo API, so the next pass launches a real Turn. It opens at most one
     at a time: none while an action todo is open, in review or blocked, or while
     the orchestrator has another open todo; the text prefix finds it again when
     the dispatcher state is lost. If two action todos for the same subject
     finish without clearing it, a user gate opens instead of a third.
     An action todo whose orchestrator Turns fail twice (the `failed`
     outcome, typically its validator, when nothing is left to do; host and
     provider failures do not count) is retired: the dispatcher closes it with
     the ordinary supersede transition, attributed to its claim owner, reports
     it under `orchestrator_todos_retired`, skips with reason
     `orchestrator_action_retired`, and opens the same repeat-limit gate, which
     holds the orchestrator until the user closes it. Before (E2E pilot v1) such
     a todo relaunched a real orchestrator Turn on every backoff expiry.
     A role_v1 goal derives neither the vision-checkpoint obligation nor the
     no-follow-up replan (decision 31, [role_v1 protocol](role-v1-protocol.md)),
     so a completed orchestrator todo no longer opens an action todo. Nor
     does an idle orchestrator: a role_v1 goal raises no
     `state_projection_gap_repair` for a Next Action that reads as a wait, and
     no periodic-review replan (decision 39). The orchestrator is then skipped
     as `orchestrator_idle`. Action todos remain for gate replies awaiting the
     orchestrator and for stall replan obligations from run history;
     escalations already arrive as S2 todos.
   - **Turn lanes (decision 32).** Under role_v1, run-once fences a registered
     developer's or acceptor's Turn per todo: its lane is (goal, todo). The
     dispatcher therefore fills a free slot of the same agent with another todo
     of the goal, up to `max_concurrency` and `--max-global`. Each such Turn is
     pinned with `--todo-id` to the todo the pass reserved for it. The
     orchestrator (lane per agent and goal, one slot) stays serial per goal.
     peer_v1 goals, and agents without a registered role, keep one in-flight
     Turn per agent and goal: the pass skips a second one with
     `turn_lane_in_flight`, which is what run-once would answer.
   - A todo that already has a running Turn is never launched a second time
     (`todo_in_flight`), whichever agent runs it; run-once's todo lane refuses a
     second executor for it as well. When the selected todo is in flight or
     cooling down for this agent, the next open (or in_review) todo from the
     same should-run lane runs instead, pinned with `--todo-id`.
   - Before the agents, every pass reopens deferred plan todos whose plan
     dependencies are all done (`plan_cards.resume_ready_plan_todos`, an
     ordinary Todo update).
   - **Deferred offers (E2E pilot v1).** Upstream should-run can still select a
     deferred todo for a developer or acceptor once its single `resume_when`
     dependency is done (`successor_replan_required`), while another plan
     dependency waits for accept+merge (decision 37). A todo-lane Turn is
     pinned, and run-once refuses a pinned deferred todo without a host call,
     so the pass never launches one: it fills the slot with the lane's next
     executable todo, or skips with reason `selected_todo_deferred` and the
     todo's `dependency_wait` from the wait read model. No backoff accrues.
5. **Auth preflight** (S3 `preflight_agent`) runs before the first launch per agent
   per pass. If it fails:
   - the agent is marked unavailable for `auth_cooldown_seconds` (default 300s);
   - one user gate opens: "Agent X needs re-login …" (`user_gate`, which blocks that
     agent). While that gate is open no second gate is created. This holds even if
     the dispatcher state is lost, because the dispatcher adopts the open gate by
     its text.
6. **Workspace.** When the selected todo has `task_repositories`, the dispatcher
   prepares a workspace for developer and acceptor Turns with S5
   `git_workspace.prepare` (one worktree per repo on branch `loopx/<goal>/<todo>`).
   - The Turn's cwd and `--project` are the worktree for a single repo, or the
     workspace root for several repos.
   - The Turn is pinned with `--todo-id`.
   - If prepare fails, that todo cools down (default 300s) and the pass reports the
     failure.
   - **Acceptor review checkout (G12).** An acceptor Turn on an `in_review` todo
     does not get the developer's worktree. The dispatcher creates a throwaway
     detached checkout of the delivered commit per repo at
     `<runtime_root>/goals/G/reviews/T/<run_id>/<repo>` and runs the Turn there
     (`review_checkout_failed` cools the todo down like a failed prepare). When
     the child is reaped, the dispatcher inspects the checkout: changes or new
     commits append an `acceptor_modified_review_checkout` warning event to the
     goal's event log and a `review_warnings` entry to its state (the role board
     flags the card); the verdict still stands. Then it removes the checkout,
     also after a crash or a failed launch.
   - **Blocked review (G12).** While an `acceptor_blocked` user gate is open for
     a todo, the acceptor is not relaunched on it: the pass picks the lane's next
     todo or skips with `review_blocked_gate_open`. The gate also blocks the
     acceptor's lane in LoopX selection, like the re-login gate.
7. **Launch** `python -m loopx.cli --registry … --runtime-root … --format json turn run-once --execute`
   with the following arguments:
   - the agent's host flags from `turn_run_once_host_arguments`;
   - `--claude-provider <provider>` for claude-code agents; for codex-cli agents
     with key-based auth, the provider credential goes into the child environment
     only;
   - a fresh `--turn-instance-id`;
   - `--timeout-seconds`;
   - `--validation-command-json`, set to the todo's own declared validation command
     when it has one. Orchestrator todos get a state check instead: an
     `action_kind=plan` todo passes once a plan card is applied
     (`loopx plan list --require-status applied`), an S2 escalation todo passes
     once the escalated todo is no longer blocked
     (`python -m loopx.dispatch.checks todo-not-status`). An orchestrator
     action todo for gates passes once none of them is open and awaiting the
     orchestrator (`checks gates-not-awaiting`). One for an effective action
     passes once some other todo was created or changed since the Turn launched
     (`checks todos-changed-since`). Otherwise `serve --validation-command-json`.

   The child's stdout and stderr go to `<runtime-root>/dispatch/runs/<run>.*`.

## Cooldowns and gates

- **Provider backoff.** A Turn can fail with host `failure_kind` `rate_limited`,
  `quota_exhausted` or `provider_capacity` (claude-code maps HTTP 429 and usage-limit
  text to these). Only that provider then enters a cooldown: 60s, 120s, 240s and so
  on, capped at 6h. Other providers keep running. A committed Turn on the provider
  resets the backoff.
- **Long cooldown.** Once a single cooldown reaches `--long-cooldown-seconds`
  (default 1h), one user gate opens: "Provider X is in a long cooldown …". It blocks
  the agent that hit the limit.
- **Host `auth_failed`** is handled like a failed preflight.
- **Todo backoff.** A todo whose Turns keep failing backs off exponentially instead
  of relaunching on every pass. The backoff is keyed by todo and agent, so a
  developer's failures do not hold back the acceptor's review of the same todo;
  a workspace-prepare failure cools the todo down for everyone. LoopX's repair and replan routing still decides what
  happens to the todo itself.
- **Push request (G8, decision 38).** Every pass over a role_v1 goal asks
  `loopx.push_requests.request_push(require_all_merged=True)`. Once no agent
  todo of the goal is `open`, `in_review` or `blocked`, and a merge target has
  this goal's merge commits (`LoopX-Goal` trailer) that its remote does not
  have yet, one `push_request` user gate opens (report key
  `gates_opened[].key=push_request`). It blocks the goal's orchestrator. At
  most one is open per goal, and a push the user rejected or cancelled is not
  offered again until new merges move a merge target. The dispatcher never
  pushes; the gate's approve does. See
  [workspaces-v0](workspaces-v0.md#pushing-merged-work-g8).

- **Usage budget (G9, decision 41).** Only goals with a budget (`loopx usage
  budget --set`) are affected. At 80% of the budget the pass opens one
  non-blocking `user_action` alert. At 100% it opens one `budget_exhausted`
  user gate per goal and budget crossing (report key `usage_budget:100`). While
  that gate is open the pass launches no new Turn of the goal for any role: each
  agent is skipped with `budget_exhausted_gate_open`. Turns already running
  finish and are reaped as usual; other goals keep running. The owner raises the
  budget, continues without a limit, or stops the goal. A goal stopped there is
  skipped with `budget_stopped_by_owner` until `loopx goal-lifecycle
  --operation resume`. See [usage-accounting-v0](usage-accounting-v0.md#budget-optional).

## Parallel Turns of one agent

When one agent has several Turns of a goal in flight, each Turn keeps its own
state:

- Turn journals are keyed by turn key, and codex-cli sessions by (goal, agent,
  todo), so they never collide. Run records live under `dispatch/runs/<run>`.
- Crash-retry and settlement-retry identities (`retry_turns` in `state.json`)
  are keyed by `<goal>/<todo>@<agent>`. A launch takes over an identity under
  the older todo-less `<goal>/<agent>` key only when it is for the same todo,
  and leaves another todo's identity in place.
- Quota spend is recorded per Turn: an effect-bound spend (the Turn pipeline's
  `<effect_id>#quota_spend`) accounts for its own Turn's delivery run, not the
  agent's latest one, which may belong to a sibling Turn.
- Settlement tolerates a sibling settling at the same moment. The quota spend
  commit is a compare-and-swap on the goal's run index; on a conflict the
  Turn rebuilds its status and retries (bounded). A refresh-state that finds
  the state file changed under it by a sibling's todo write retries from the
  current state before persisting anything (bounded). refresh-state itself
  runs under the goal's run-index lock.

`hard_lease` canonical goals are out of scope: the lane change does not touch
leases, and `in_review` stays unsupported there (decision 33).

## Crash recovery

A child that exits without a JSON payload, or that is killed by a signal, counts as
`crashed`. The next pass relaunches the same agent and todo with the same Turn
identity:

- If run-once already journaled that Turn, the dispatcher passes
  `--resume-turn-key <key> --retry-failed-turn`. run-once then replays what was
  settled and continues from the last side-effect-safe phase. It never invokes the
  host again for a settled Turn, and never settles twice.
- Otherwise the dispatcher reuses `--turn-instance-id`. The settlement effect id is
  derived from it, so a second writeback of the same effect is rejected.

After 3 crashes in a row the todo backs off.

### Settlement that fails after the host completed

The bounded settlement retries above can run out under sustained contention.
The child then exits with a failed payload, not a crash: either an error (the
retry raised, and the journal stays `in_progress` with a prepared effect) or
`status=failed` with a receipt whose `failed_phase` is a settlement phase
(`durable_writeback`, `quota_spend`, `terminal_closeout`, `scheduler_*`). In
both cases the journal already holds the host's typed result.

The dispatcher keeps that Turn's identity. It records a `retry_turns` entry
(`settlement: true`, the journal key, the failed phase and the delivery
workspace the host ran in), and the todo takes the ordinary failure backoff.
Once the backoff ends, the next pass resumes the Turn before asking
should-run (launch reason `settlement_retry`), because the todo may already be
`in_review` and would not be selected again. The resume passes
`--resume-turn-key <key> --retry-failed-turn` from the same workspace, so
run-once skips the host (`typed_result` is complete) and settles the cached
result: the quota spend is kept and the host does not redo the work.

A validation or host failure is not a settlement failure; it keeps starting a
new host attempt. After 5 settlement resumes that still fail, the dispatcher
drops the identity and the next launch mints a new Turn, as before.

Children run in their own session. They survive a dispatcher restart and are
adopted on the next start: the dispatcher tracks their pid, and reads their output
file once they exit.

## Result hygiene

The executor's public-safety check rejects local absolute paths in result fields.
Two layers tell every Turn to use repo-relative paths instead:

- the Turn prompt shared by both built-in hosts
  (`codex_cli.RESULT_PATH_HYGIENE_INSTRUCTION`);
- for claude-code, a per-run system prompt that the dispatcher composes. It contains
  the agent's own `system_prompt_file` followed by a dispatcher addendum: the role,
  the workspace repos, commit-but-don't-push for developers, and review-only for
  acceptors. The orchestrator's addendum names the exact CLI prefix
  (interpreter, `--registry`, `--runtime-root`) for its gate and plan commands,
  how to open a question gate, to return `user_action_required` while it waits
  on the user, and how to resolve an escalation. When it replaces or splits a
  todo it uses `loopx todo supersede --by`, never marking the replaced todo
  done (gap G6).

Role guidance that every host needs lives in the shared Turn prompt instead: an
`in_review` todo tells the acceptor that it only reviews and never modifies
code, that `validated_completion` accepts, `repair_required` rejects (with
the required changes as feedback) and `user_action_required` is the blocked
verdict when it cannot review (G12), and a reopened todo shows the developer
the acceptor's `review_feedback`. Both developer and acceptor see the todo's
`acceptance_criteria` and the goal acceptance contract (gap G2); the acceptor
must name each criterion that failed when it rejects.

## Files

`<runtime-root>/dispatch/`:

- `serve.lock`
- `state.json`: running children, history, provider, agent and todo cooldowns,
  opened gates, budget alerts, orchestrator action todos, crash-retry identities, orchestrator
  baselines, per-agent slots and acceptor review warnings (G12).
- `runs/`: child output.
- `logs/`: launchd output.

## Known gaps

- `turn run-once` needs an independent validator for material results. Todos with
  no declared validation command need `serve --validation-command-json`. Without it,
  their Turns fail validation and the todo backs off.
- The `todos-changed-since` validator is coarse: another agent's concurrent todo
  write also satisfies it. If an orchestrator's Turns do not clear a remaining
  replan obligation from run history, the repeat limit turns it into a user gate.
  The vision-checkpoint and no-follow-up obligations no longer reach this path
  (decision 31), nor do the idle orchestrator's projection repair and the
  periodic review (decision 39).
- Acceptor assignment (decision 5) and `in_review` (S2) are LoopX selection
  concerns. The dispatcher simply runs an acceptor when `should-run` gives it a
  todo.
- The dispatcher selects an agent's config with its registry role, so the
  acceptor's unsandboxed role default (G12) applies whatever the file's `role`
  says, unless the file sets `sandbox` or `permission_mode` explicitly.
