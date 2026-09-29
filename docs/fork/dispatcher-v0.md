# Dispatcher (S4)

Implements design decisions 2-4 and 19 of [design-v0](design-v0.md). It is also where decision 17 (auth preflight) and decision 18 (per-todo workspaces) take effect at launch time. Code
lives in `loopx/dispatch/`; the CLI is `loopx dispatch`.

The dispatcher is a scheduler. It is not a second state machine. For each goal and
agent it asks LoopX whether the agent should run now (the quota `should-run`
decision, called through the Python API). It then applies slot, cooldown and auth
rules, and launches `loopx turn run-once` as a child process. The Turn pipeline owns
the typed result, validation, idempotent writeback and quota spend. The dispatcher
never writes goal state directly. Its only goal writes are user gates (re-login,
long cooldown, orchestrator repeat limit, push request, budget exhausted and goal
complete),
which it creates through the normal Todo API (`add_goal_todo`, the same path as
`todo add --role user --task-class user_gate`), and the completion of orchestrator
todos that are pure bookkeeping (decision 43, below), through the normal
`complete_goal_todo`.

## Running it

```sh
# one reconcile pass; waits for the Turns it launched (cron / tests)
loopx dispatch serve --goal-id G --once

# resident: watch state files, reconcile on changes and every tick
loopx dispatch serve --goal-id G [--goal-id G2 ...] [--project P] \
  [--tick-seconds 60] [--poll-seconds 3] [--max-global 4] \
  [--turn-timeout-seconds 3600] [--long-cooldown-seconds 3600] \
  [--backoff-base-seconds 60] [--validation-command-json '["make","test"]'] \
  [--idle-heartbeat-seconds 900]

loopx dispatch status            # running Turns, per-agent slots, cooldowns, gates, project-directory waits
loopx dispatch launchd-plist --goal-id G > ~/Library/LaunchAgents/com.loopx.dispatch.plist
launchctl load ~/Library/LaunchAgents/com.loopx.dispatch.plist   # you install it; the command only prints
```

The global `--registry` and `--runtime-root` options select the state home as usual.
The dispatcher makes the registry path and the runtime root absolute once, so every
Turn, its validator and the launchd job find them from their own working directory:
a todo worktree, a review checkout or the runtime root. Symlinks are not resolved,
because registry writes replace and lock the path as given.
Only one dispatcher can run per runtime root. `serve` and `serve --once` take an
exclusive `flock` on `<runtime-root>/dispatch/serve.lock`. A second dispatcher on the
same runtime root exits with code 3 (`dispatcher_locked`).

`serve` prints a pass as one JSON line only when it acts (launches, reaps, opens a
gate or errors). So that an idle goal does not look like a dead dispatcher (pilot v1
gap N11), it prints one `loopx_dispatch_idle_heartbeat_v0` line when nothing was
printed for `--idle-heartbeat-seconds` (default 900, `0` disables): the idle time,
the passes since the last line, their skip reasons (at most 8) and the running
Turns. It is a log line only.

The plist starts the dispatcher with `KeepAlive` and `RunAtLoad`, and uses the Python
interpreter that rendered it. It copies the current `PATH`, so `claude`, `codex` and
`git` resolve the same way they do in your shell. It sets `PYTHONPATH` to the source
root of the `loopx` package that rendered it, ahead of any `PYTHONPATH` entries
already set. The job therefore runs that code, such as an installed release
snapshot, and not whatever the interpreter's site-packages resolves, such as an
editable install of a checkout. The Turns it starts inherit the same `PYTHONPATH`.
After an upgrade, render and load the plist again. Its logs go to
`<runtime-root>/dispatch/logs/`.

## What one reconcile pass does

1. **Reap** finished children. The pass classifies each child from its exit code and
   its `--format json` output: `committed`, `host_failed` (with the host's
   `failure_kind`), `failed` or `crashed`. The reaped entry of a failed child
   also carries run-once's `error_code` and its `error` (else its `reason`): one
   line of at most 300 characters. The runtime root, state home, `--project` and
   home directory, as given and resolved, are replaced first by `<runtime-root>`,
   `<state-home>`, `<project>` and `<home>`, even when they hold spaces. Other
   absolute paths (POSIX, `~/`, drive and UNC paths, `file://` URLs, quoted paths)
   then become `<path>`; an unquoted path with spaces outside those roots may be
   masked only in part. The credentials the host stderr tail below redacts are
   redacted last. The text stays in your local dispatcher log and status.
   `failure_kind` stays the host's typed kind, so it is empty when the Turn
   failed before or after the host call.
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
     no periodic-review replan (decision 39), nor for a Next Action that reads
     as executable work once no agent todo is open (decision 42: the last Turn
     to settle wrote it, typically the acceptor's "Settle todo_X as accepted";
     pilot v1 gap N2). The orchestrator is then skipped as `orchestrator_idle`,
     and a finished goal gets the goal_complete gate below instead. Action todos remain for gate replies awaiting the
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
   - **One project-directory Turn per goal.** A developer or acceptor Turn on a
     todo without `task_repositories` gets no workspace (step 6): it works in
     the goal's project directory, where no worktree, review checkout, merge
     or rollback separates it from another such Turn. So per goal at most one
     of them runs at a time (`policy.TurnWorkspace`; each run records
     `workspace`: `project_directory` or `todo_workspace`). While one runs, a
     free developer or acceptor slot takes the lane's next todo that names
     repos, pinned with `--todo-id`, or skips with
     `project_directory_turn_running` (with `running_run_id` and
     `running_todo_id`). The classification reads the todo records, not
     should-run's compacted items. Orchestrator Turns, Turns on todos with
     repos, and settlement resumes (which run no host) are not affected.
     `dispatch status` shows the running Turn as in the project directory and
     the last pass's waits (`last_pass.project_directory_waits`).
   - Before the agents, every pass reopens deferred plan todos whose plan
     dependencies are all done (`plan_cards.resume_ready_plan_todos`, an
     ordinary Todo update). A released todo whose branch was cut earlier and
     has no commits of its own is fast-forwarded to the current merge target
     (pilot v1 gap N10, see [workspaces-v0](workspaces-v0.md#stale-todo-branches-on-release-pilot-v1-gap-n10));
     the pass reports it under `todo_branches_refreshed`.
   - **Mechanical bookkeeping (decision 43).** Before it launches the
     orchestrator for a selected todo, the pass checks whether the todo is pure
     bookkeeping (`loopx.orchestrator_bookkeeping`). If so it completes the
     todo itself, attributed to the orchestrator (its claim owner), with the
     evidence the Turn would have reported, reports it under
     `orchestrator_todos_closed` (`kind`, `evidence`) and asks should-run again
     instead of launching a Turn:
     - `planning_closeout`: an open planning todo (`action_kind=plan`,
       `required_role=orchestrator`) once a plan card that creates todos is
       applied. This normally already happened when the plan was applied (see
       [gates-plans-intake-v0](gates-plans-intake-v0.md)); the pass is the
       backstop for goals applied before the change or a failed closeout. A
       planning todo that an applied plan created itself is left to the
       orchestrator.
     - `escalation_target_closed`: an S2 escalation todo whose escalated todo is
       already `done` (accepted and merged, superseded, or closed by the owner).
     - `gate_action_answered`: a gate-reply action todo when every listed gate
       was already answered by the orchestrator (its thread's last message is
       the orchestrator's) or is a closed LoopX system gate (`acceptor_blocked`,
       `push_request`, `budget_exhausted`, `goal_complete`), whose decision
       LoopX settled and whose follow-up work, if any, arrives as its own todo.

     Everything else stays a model Turn, deliberately: planning and
     clarification, a gate reply the orchestrator has not answered (also when
     the user closed a question or plan gate after replying), an escalation
     whose todo is still blocked, a criteria-change outcome, a replan
     obligation and a user follow-up. At most 5 todos are closed per
     orchestrator and pass; then the pass skips with
     `orchestrator_bookkeeping_limit` and the next pass continues. peer_v1
     goals are unchanged. A closeout that fails is reported under `errors` and
     the Turn launches as before.
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
   A todo that names none runs in the goal's project directory and delivers
   from there ([workspaces-v0](workspaces-v0.md#delivery-identity-decision-29)).
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
- **Unavailable upstream (pilot v1 gap N9).** codex-cli maps what a proxy such as
  CLIProxyAPI reports for an unavailable upstream to `provider_capacity`: an HTTP
  500, 502 or 504 status, a `5xx` status in an error message, `auth_unavailable`,
  `dial upstream`, a refused or reset connection, or an `error sending request`.
  The Turn is retryable, and the dispatcher backs off the provider, not the todo.
  A structured HTTP 503 stays `provider_overloaded`, as before. On a failed or
  timed-out Turn the codex host writes the last 20 stderr lines to the Turn's own
  stderr (`runs/<run>.err.log`), each at most 400 characters and redacted first:
  bearer/basic credentials, `Authorization` headers, key/token/secret/password
  assignments, `sk-`/JWT-shaped and other long opaque strings, URL user info and
  absolute local paths never reach the log. Nothing of it is persisted in Turn
  journals or events.
- **Long cooldown.** Once a single cooldown reaches `--long-cooldown-seconds`
  (default 1h), one user gate opens: "Provider X is in a long cooldown …". It blocks
  the agent that hit the limit.
- **Host `auth_failed`** is handled like a failed preflight.
- **Todo backoff.** A todo whose Turns keep failing backs off exponentially instead
  of relaunching on every pass. The backoff is keyed by todo and agent, so a
  developer's failures do not hold back the acceptor's review of the same todo;
  a workspace-prepare failure cools the todo down for everyone. LoopX's repair and replan routing still decides what
  happens to the todo itself. The backoff keeps the last failed Turn's error, and
  `dispatch status` lists each todo cooldown with its failure count, its end time
  (UTC) and that error.
- **Push request (G8, decision 38).** Every pass over a role_v1 goal asks
  `loopx.push_requests.request_push(require_all_merged=True)`. Once no agent
  todo of the goal is `open`, `in_review`, `blocked` or `deferred` (the same
  unfinished statuses as the goal complete gate below), and a merge target has
  this goal's merge commits (`LoopX-Goal` trailer) that its remote does not
  have yet, one `push_request` user gate opens (report key
  `gates_opened[].key=push_request`). It blocks the goal's orchestrator. At
  most one is open per goal, and a push the user rejected or cancelled is not
  offered again until new merges move a merge target. The dispatcher never
  pushes; the gate's approve does. See
  [workspaces-v0](workspaces-v0.md#pushing-merged-work-g8).

- **Goal complete (decision 42, pilot v1 gap N2).** After the push request,
  every pass over an active role_v1 goal asks
  `loopx.goal_complete_gate.open_goal_complete_gate`. Once the goal's work is
  finished, one `goal_complete` user gate opens (report key
  `gates_opened[].key=goal_complete`), with no model Turn. Finished means:
  - no agent todo is `open`, `in_review`, `blocked` or `deferred`, and no user
    gate is open (a pending push gate included, so the push gate comes first);
  - at least one non-orchestrator agent todo is done, and every done todo that
    requires acceptance carries an accept record (accepted and merged,
    decision 37); superseded todos never count;
  - the push is resolved in every repo: pushed, rejected or cancelled at the
    push gate, or nothing to push (no remote, no merges, no unpushed merges of
    this goal). A goal without a remote gets the gate right after its last
    merge.

  The gate is computed without a model: per repo the merge target, the merged
  todo commits (`LoopX-Goal` trailer) and the push result; the accepted,
  reject and superseded counts; the `loopx usage report` totals (reported and
  estimated cost, Turns, agent-hours, cost per accepted todo) and per-role
  split; open follow-ups (open user todos that are not gates). It blocks the
  orchestrator. One completion (its done agent todos and merge target heads,
  `completion_key`) opens at most one gate, across replays, restarts and a lost
  state file; an open gate that lost its index entry is adopted by its text.
  Options: `close_goal` stops the goal through `loopx goal-lifecycle`, and the
  pass then skips every agent of the goal with `goal_closed_by_owner` (no push,
  budget or completion work either) until it is resumed; `add_work` turns the
  note into one "Orchestrator action: User follow-up: …" todo, which launches
  an ordinary orchestrator Turn (validator `todos-changed-since`); `leave_open`
  changes nothing, and no further goal_complete gate opens until new work
  finishes. `close_goal` is refused when the goal changed after the gate
  opened. See [gates-plans-intake-v0](gates-plans-intake-v0.md).

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
  done (gap G6). It ends with the goal state digest (decision 43, below).

Role guidance that every host needs lives in the shared Turn prompt instead: an
`in_review` todo tells the acceptor that it only reviews and never modifies
code, that `validated_completion` accepts, `repair_required` rejects (with
the required changes as feedback) and `user_action_required` is the blocked
verdict when it cannot review (G12), and a reopened todo shows the developer
the acceptor's `review_feedback`. Both developer and acceptor see the todo's
`acceptance_criteria` and the goal acceptance contract (gap G2); the acceptor
must name each criterion that failed when it rejects.

## Orchestrator state digest (decision 43)

Every orchestrator Turn's addendum ends with a goal state digest that the
dispatcher renders at launch (`loopx.dispatch.orchestrator_digest`). Design
decision 2 asks for it: the orchestrator reads its context from LoopX state
every Turn, so what it gets must be compact. Before, it got nothing and
discovered the state itself: the E2E pilot v1 orchestrator Turns ran 26 to 43
host steps, mostly CLI reads (`status`, `todo list`, `gate show`, `plan show`),
and each step re-read 40 to 60k tokens of context (0.6 to 1.8M cached input
tokens, $1 to $2.6 per Turn).

The digest is built from the read models the CLI prints (`list_goal_todos`,
the gate index and threads, plan cards, the live plan dependency waits, the
rollout event log tail), never from dispatcher state. It contains:

- why this Turn runs and for which todo (planning, escalation of a named todo,
  gate replies awaiting the orchestrator, action todo), with the todo's text;
- the goal objective, the requirements doc and the goal acceptance contract
  summary; the repos (merge target, default branch) and the agents' roles;
- open user gates of every kind (question, `plan_approval`, `acceptor_blocked`,
  `push_request`, `budget_exhausted`, `goal_complete`, dispatcher gates),
  gates awaiting the orchestrator first, each with the last 2 thread messages
  (4 when it awaits the orchestrator);
- pending plan cards and the last applied one;
- live todos (blocked, in_review, open, deferred, then state-file order) with
  required role, bound agent, acceptor, reject count, repos, acceptance
  criteria, dependency waits, review feedback and delivery state; then
  closed todos, newest first, as accepted and merged, superseded or done;
- recent orchestrator-relevant events, newest first (plan, supersede, review
  gate, push, goal complete, budget and acceptance transitions; no quota or
  refresh events).

**Bounds.** Every field is clipped (criteria 280 and feedback 320 characters,
except the todo the Turn is about, which shows its whole criteria and
feedback). Each section has an item cap and a character budget (gates 5,500,
plans 1,000, live todos 7,500, closed todos 1,600, events 1,400); it keeps
whole items in its order and ends with `- … N more X omitted (CLI hint)`. The
rendered text is also cut at 20,000 characters (about 5k tokens) on a line
boundary with a truncation marker. The same state renders the same text. The
pilot v1 goal renders in about 3.3k to 4.6k characters (about 1k tokens); a
300-todo goal with 15 long gate threads in about 14k.

**Guidance and staleness.** The addendum tells the orchestrator that the
digest is current as of launch, to start from it rather than re-read state it
shows, to use CLI reads only for detail it omits or truncates (`gate show`,
`todo list --todo-id`, `plan show`), and to write only through the CLI as
before. The digest is advisory: the Turn pipeline's validation, the dispatcher's
Turn validators and each CLI write's own checks still guard correctness, so a
write against a state that changed since launch is refused as before. If the
digest cannot be built, the addendum carries a one-line note instead and the
Turn runs.

Developer and acceptor Turns get no separate digest: their Turn prompt already
carries the selected todo, its acceptance criteria, review feedback and the
goal acceptance contract, and the addendum names their workspace repos.

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
  periodic review (decision 39), nor a stale executable Next Action on a
  finished goal (decision 42).
- Acceptor assignment (decision 5) and `in_review` (S2) are LoopX selection
  concerns. The dispatcher simply runs an acceptor when `should-run` gives it a
  todo.
- The system-prompt addendum, and with it the orchestrator state digest, only
  reaches claude-code agents (`--claude-system-prompt-file`). A codex-cli
  orchestrator gets neither; the shared Turn prompt is unchanged.
- The dispatcher selects an agent's config with its registry role, so the
  acceptor's unsandboxed role default (G12) applies whatever the file's `role`
  says, unless the file sets `sandbox` or `permission_mode` explicitly.
