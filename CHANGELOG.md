# Changelog

This file records the changes of this LoopX fork. The fork branched from
upstream [`loopx-project/loopx`](https://github.com/loopx-project/loopx) at
`71dbfd5e6` (2026-09-26) and no longer tracks upstream
([design-v0](docs/fork/design-v0.md)). For upstream history before that
point, see the upstream releases and [docs/update-notes](docs/update-notes/README.md).

The package version in `pyproject.toml` is still the upstream `1.2.0`. Until the
fork cuts its own tag, its changes are listed under **Unreleased**. Pull request
numbers refer to [michaelx1993/loopx](https://github.com/michaelx1993/loopx/pulls).

To learn how to use these features, read the [usage guide](docs/fork/usage.md).

## [Unreleased] - Fork v0: role-based multi-agent orchestration

Covers the fork's first round, 2026-09-25 to 2026-09-28 (PRs #1 to #29,
136 non-merge commits), and the fixes that followed it.

### Highlights

- **The orchestrator role.** In upstream `peer_v1`, every worker agent plans,
  decomposes and claims work for itself, and a hash picks who owns a replan.
  The fork adds the `role_v1` runtime model, which has three registered roles:
  - at most one **orchestrator** per goal. It clarifies requirements with
    you, plans, owns the acceptance criteria, answers escalations, and is
    the only role-assigned agent that opens user gates;
  - **developers**, which implement todos;
  - **acceptors**, which only review.

  `role_v1` is the default for new goals.
- **A resident dispatcher.** `loopx dispatch serve` launches role Turns when
  state changes and on a periodic tick. It only schedules: every write still
  goes through the existing CLI and kernel contracts.
- **You talk to the orchestrator through gates.** Every user gate has a
  discussion thread. The initial plan arrives as a plan card that you
  approve, and so does an agent's later change to a todo's acceptance
  criteria. `loopx goal create` turns a requirements document into a running
  goal.
- **Review before merge.** A delivery that requires acceptance moves to the
  new `in_review` status. By default, that is developer implementation work
  on a goal that has an acceptor. An acceptor then accepts, rejects or
  blocks it. Accepted work merges atomically across the todo's repos. The
  second rejection of a todo escalates it to the orchestrator.
- The fork also adds multi-repo git workspaces, a push gate, a goal-complete
  gate, per-Turn cost accounting with optional budgets, and a role board in
  the dashboard.
- Two end-to-end pilots on real models validated the full loop
  ([pilot v0](docs/fork/e2e-pilot-report-v0.md),
  [pilot v1](docs/fork/e2e-pilot-report-v1.md)).

### Added

#### Roles and the orchestrator (#4, #7, #27)

- **Roles in the registry.** `coordination.agent_model` is `role_v1` or
  `peer_v1`. `coordination.agent_roles` maps each registered agent to
  `orchestrator`, `developer` or `acceptor`, and a goal has at most one
  orchestrator. Set them with:
  - `loopx register-agent --role ROLE`;
  - `loopx configure-goal --agent-role ID=ROLE`, `--clear-agent-role ID` or
    `--agent-model role_v1|peer_v1`.
- **Role fields in the todo contract.** The contract gains `required_role`,
  `requires_acceptance`, `acceptor_agent`, `reject_count` and
  `task_repositories`, followed later by `delivered_by`, `review_feedback` and
  `acceptance_criteria`. The matching flags:
  - `loopx todo add` and `loopx todo update` take `--required-role`,
    `--requires-acceptance`, `--acceptor-agent`, `--reject-count`,
    `--task-repo` and `--acceptance-criteria`.
  - Only `loopx todo update` takes `--review-feedback` and the clear flags:
    `--clear-required-role`, `--clear-acceptor-agent`, `--clear-task-repos`,
    `--clear-acceptance-criteria` and `--clear-review-feedback`.
- **Role-aware selection.** An agent with a registered role only receives
  todos whose effective role matches its own. An agent without a role keeps
  the flat peer routing. User gates, blockers and planning work belong to the
  orchestrator, so it never receives implementation work. An acceptor
  receives the `in_review` todos it is the resolved acceptor for, and todos
  tagged `required_role=acceptor`.
- **Replan routing.** Replan obligations go to the goal's orchestrator
  instead of the owner that the sha256 peer hash picks.
- **Only the orchestrator opens user gates (#7).** Under role_v1, agents with
  the developer or acceptor role raise `blocker` todos instead. The
  orchestrator answers them itself, or turns them into a gate with options
  and a recommendation. Agents without a role keep the upstream behavior.
- **Less orchestrator overhead (decision 43, #27).** LoopX now completes pure
  bookkeeping without a model Turn: the planning todo once its plan is
  applied, an escalation whose todo is already done, and a gate-reply action
  todo whose gates were already answered. Every claude-code orchestrator Turn
  also starts from a bounded goal state digest (at most 20,000 characters),
  so it needs fewer CLI calls to discover state.

#### Dispatcher (#6, #14, #23, #24)

- **`loopx dispatch serve | status | launchd-plist`.** For each goal and
  agent, a pass asks LoopX's `should-run` whether the agent has work. It then
  applies slots, auth preflight and backoff, and launches
  `loopx turn run-once` with the agent's host flags.
  - Slots: the orchestrator is serial per goal. Developers and acceptors run
    up to their `max_concurrency`, and every Turn counts against a
    machine-wide `--max-global`.
  - Only one dispatcher runs per runtime root. It holds a `flock`, and a
    second one exits with code 3.
  - `launchd-plist` prints a `KeepAlive` job for macOS and installs nothing.
- **Per-todo Turn lanes (decision 32, #14).** One developer can work on
  several todos of the same goal in parallel. A single todo never has two
  Turns at once.
- **Failure handling.**
  - A provider that hits rate limits or its quota backs off on its own:
    60 s, then doubling, capped at 6 h.
  - A todo whose Turns keep failing backs off exponentially.
  - A crashed Turn is replayed with the same Turn identity, so it never
    settles twice.
  - A Turn whose settlement ran out of retries is resumed from its journal
    without calling the host again (#17).
- **Orchestrator action todos.** When the orchestrator has work that no todo
  carries, such as a gate reply, the dispatcher opens one action todo. It
  retires an action todo whose Turns keep failing, and opens a repeat-limit
  gate (#23).
- **Idle heartbeat (#24).** An idle dispatcher prints a heartbeat line every
  `--idle-heartbeat-seconds` (default 900), so it no longer looks dead.
- **System gates.** LoopX itself opens these gates, each through the normal
  Todo API: re-login needed, long provider cooldown, orchestrator repeat
  limit, `acceptor_blocked`, `push_request`, `budget_exhausted` and
  `goal_complete`.

#### Gate threads, plan cards and goal intake (#2, #7, #20)

- **Gate threads.** `loopx gate reply | show | list | resolve`. Every user
  gate has an append-only discussion thread with an awaiting state
  (`awaiting_user` or `awaiting_orchestrator`). Your reply wakes the
  orchestrator, and replying never closes a gate.
- **Plan cards.** `loopx plan propose | show | list | apply`.
  - The orchestrator proposes a plan as JSON: its todos, roles, repos,
    dependencies, acceptance criteria and validation commands.
  - The plan waits on a `plan_approval` gate. The orchestrator revises it in
    place with `--revise`.
  - On approval, LoopX creates the plan's todos, and dependent todos start
    `deferred`. A Markdown goal writes them all in one locked write. A
    canonical goal creates them in order with idempotent operation ids, so an
    interrupted apply resumes without duplicating todos.
- **Criteria changes (decision 40, #20).** A plan card can carry
  `criteria_changes` for existing todos. You see the old and new criteria side
  by side. While the change is pending, the acceptor does not review the
  affected todo.
- **Goal intake.** `loopx goal create --project STATE_HOME --goal-id G
  --doc req.md --repo NAME=PATH --agent ID=ROLE` does the whole setup:
  - bootstraps the goal;
  - registers the requirements doc as an authority source;
  - registers the agents, their roles and the repos;
  - adds the orchestrator's first planning todo.
- **Web gate decisions (#2).** The dashboard's `gate.resolve` now applies
  through the CLI decision path. It supports approve, reject and cancel with
  an optional note (at most 600 characters), and shows a readback afterwards.
  On `acceptor_blocked`, `budget_exhausted` and `goal_complete` gates, each
  button names the option it submits, such as "Approve (raise_budget)", and
  only the options the gate lists are offered.
- **Web discussion panel.** The gate drawer lists the thread and has a reply
  box. Two loopback-only endpoints back it: `GET /api/chat/gate-thread` and
  `POST /api/chat/gate-thread/reply`.

#### Acceptance flow (#8, #11, #13, #16)

- **The `in_review` status.** On a role_v1 goal that has registered roles,
  completing an open agent todo that requires acceptance does not mark it
  done. A goal without roles, and user todos, keep the direct completion.
  Which todos require it:
  - with `requires_acceptance=true`, always;
  - with `requires_acceptance=false`, never;
  - without the flag, only a developer advancement todo, and only when the
    goal has an acceptor: a bound `acceptor_agent`, or a `role=acceptor`
    agent.

  LoopX first runs the todo's declared validation command, then moves the
  todo to `in_review`. It records who delivered it and, for a todo with
  repos, the commit it delivered in each repo (`delivered_shas`).
- **Verdicts.** `loopx todo accept | reject | block-review`. An acceptor's
  Turn result maps to the same verdicts:

  | Turn result | verdict |
  |---|---|
  | `validated_completion` | accept |
  | `repair_required` with a summary | reject |
  | `user_action_required` | blocked |

  - Accept completes the todo. For a todo with repos, it merges first and
    completes the todo only after the merge.
  - Reject reopens the todo for the same developer. The feedback is required,
    and the prompt asks it to name each failed criterion.
  - The second rejection blocks the todo and opens an escalation todo for
    the orchestrator.
- **Per-todo acceptance criteria (G2, #11).** The criteria live in a field
  that only the orchestrator, or the owner without an `--agent-id`, writes.
  Agents without a registered role keep the upstream behavior. Developer and
  acceptor Turns see the criteria every time. Rework instructions go to
  `review_feedback`.
- **Acceptor isolation (G12, #13).** For a todo with `task_repositories`:
  - the acceptor reviews in a throwaway detached checkout of the delivered
    commit, never in the developer's worktree;
  - the merge takes exactly the delivered commit, and a branch that moved
    after delivery blocks the merge;
  - changes the acceptor makes to its checkout are detected, recorded and
    discarded.

  A todo without repos is reviewed in the goal project, as before, and has
  no merge. By default the acceptor runs without a sandbox, so it can build
  and test.
- **The `acceptor_blocked` gate.** When the acceptor cannot review, for
  example because tooling is broken, you resolve the gate with one option:
  `retry_acceptance`, `accept_manually`, `return_to_developer` or
  `cancel_todo`.
- **Dependency release and supersession (decision 37, #16).**
  - On a role_v1 goal, a dependency that requires acceptance releases its
    dependents only once it is accepted. Accepting a todo with repos merges
    it first.
  - To replace or split a todo, run
    `loopx todo supersede --goal-id G --todo-id OLD --by NEW[,NEW2]`. This
    rewires the dependents, and a superseded todo never counts as done.
  - `loopx todo list` explains why a deferred todo still waits
    (`dependency_waits`).

#### Git workspaces, merge and push (#1, #12, #19, #25)

- **Goal repos.** Declare a goal's repos with `loopx configure-goal --repo
  NAME=PATH[,default_branch=B][,merge_target=main|task_branch][,task_branch=B]`.
  A goal without a repo list keeps its legacy single `repo`, which acts as the
  repo `main`.
- **Workspaces.** `loopx workspace prepare | status | merge | cleanup`. A
  todo gets one worktree per selected repo, all on the branch
  `loopx/<goal>/<todo>`. A manual command selects the repos given with
  `--repo NAME`, else the todo's `task_repositories`, else every goal repo.
  The dispatcher prepares workspaces only for todos that name repos.
  - The merge is atomic across the todo's repos: if any repo fails, none is
    merged.
  - Each repo with changes gets a no-ff merge commit with `LoopX-Goal` and
    `LoopX-Todo` trailers.
  - Each repo's merge target is its default branch (`merge_target=main`, the
    default) or a task branch (`merge_target=task_branch`, named
    `loopx-task/<goal>` unless `task_branch` is set).
- **Delivery identity (decision 29, #12).** A multi-repo Turn settles against
  a todo workspace identity. A repo without `origin` gets a local `repo_id`,
  so it works without a fake remote.
- **Push gate (decision 38, #19).** Once all of a goal's work is merged and a
  merge target has goal commits that its remote lacks, one `push_request`
  gate lists them per repo. A repo without a remote is skipped, and a goal
  whose repos are all local opens no gate.
  - Approving it pushes each merge target. It never force-pushes.
  - The orchestrator or you can ask for the gate earlier with
    `loopx goal request-push`.
  - An optional `on_push_command`, such as `gh pr create --fill`, runs after
    each successful push.
- **Goal-complete gate (decision 42, #25).** A finished goal gets one
  deterministic `goal_complete` gate, with no model Turn. It shows the merges,
  the push results, the review counts and the cost. Its options are
  `close_goal`, `add_work` and `leave_open`.

#### Agent and provider configuration, Turn hosts (#3)

- **Provider config.** `<runtime-root>/providers.yaml` defines providers of
  kind `anthropic`, `openai`, `openai-compatible` or `codex-cpa`. Auth is
  `api_key`, `oauth_cli` or `oauth_token`. A provider names an env var, a
  keychain entry or a CLI login, and LoopX refuses secret values in the
  file.
- **Agent config.** Each agent has a file at `<runtime-root>/agents/<id>.yaml`.
  A project can override it field by field in `.loopx/agents/<id>.yaml`. The
  file sets the role, runtime, provider, model, effort, system prompt,
  permission or sandbox mode, `max_concurrency` and extra args.
- **Inspection and preflight.** `loopx agent list | show | validate` and
  `loopx provider list | check`. Before the first launch, the dispatcher runs
  an auth preflight for each agent.
- **The `claude-code` Turn host.** `loopx turn run-once --host claude-code`
  takes `--claude-model`, `--claude-permission-mode`, `--claude-effort`,
  `--claude-system-prompt-file`, `--claude-extra-arg`, `--claude-provider` and
  `--claude-bin`. Each Turn is a fresh `claude -p` session.
- **codex-cli config overrides.** codex-cli gains `--codex-config KEY=VALUE`,
  which is passed through as `-c`, and `--codex-provider NAME`, which expands
  a provider from `providers.yaml`. This is how you use CPA.

#### Usage and budget (#15, #22, #24)

- **Turn usage.** Every claude-code and codex-cli Turn records a `turn_usage`
  block: tokens, cost and host steps when the host reports them, and the
  duration. A Turn that times out or crashes without host accounting records
  its duration only. A crash before the ledger write is recorded when the
  Turn is replayed. The rows go to an append-only
  `<runtime-root>/goals/<goal>/usage.jsonl`.
- **Estimated pricing.** A provider can declare a `pricing` table, which
  estimates the cost of hosts that report no USD, such as Codex through CPA.
- **Reports.** `loopx usage report [--by role|agent|goal|todo|model|day]` shows
  agent-hours, cost and Turns. It also shows the cost per accepted todo, with
  and without the orchestrator's share. The status projection carries the same
  metrics as `turn_usage_summary`, over the latest 5,000 ledger rows.
- **Budgets (decision 41, #22).** Budgets are optional:
  `loopx usage budget --goal G --set USD`.
  - At 80% of the budget, LoopX opens a non-blocking alert.
  - At 100%, a `budget_exhausted` gate pauses the goal's new Turns, and
    running Turns finish. Resolve it with `raise_budget`,
    `continue_without_limit` or `stop_goal`.

#### Dashboard (#9 and later)

- **The role board tab (角色看板).** It opens with a "Waiting on you" list of
  gates and plan approvals. Below that, columns Planned, Assigned, Running,
  In review, Rework and Done are split into orchestrator, developer and
  acceptor swimlanes.
- **Cards.** A card shows its agent, repos, reject count, acceptor,
  acceptance criteria, why it waits on dependencies, and its plan link.
- **The usage strip.** It shows cost, agent-hours, cost per accepted todo and
  the budget share.
- **The gate drawer.** It shows the discussion thread and reply box. While
  it is open and the page is visible, it re-reads the gate every 5 s and on
  focus, so a reply or decision made from the CLI shows up without reopening
  it. Above the thread, a card per gate kind shows what you are deciding:
  - a plan approval shows the plan card, with each todo's contract, and its
    criteria changes, old and new side by side;
  - a push request shows each repo's branch, remote and commits;
  - a budget gate shows the spend against the budget and the default raise;
  - a goal-complete gate shows the review counts, merges, push results and
    cost.

  Decisions are enabled only after a successful read of the gate the drawer
  shows, and only while that gate is open. A failed refresh disables them
  until the next successful read. A part that cannot be read is shown as
  unavailable, never as empty. The role board shows each gate's real kind,
  and `loopx gate show` prints the same plan card.

### Changed

These behaviors differ from upstream.

- **Runtime model.** New goals, and goals that record no model, run
  `role_v1`. Goals that already record `peer_v1` keep the flat peer behavior
  until you run `loopx configure-goal --agent-model role_v1`.
- **Anti-hierarchy validators removed.** `agent_profiles.*.profile_role`
  accepts labels such as `orchestrator`, `manager` and `worker`. On role_v1
  goals, the legacy hierarchy detector no longer treats an
  `agent_profiles.*.role` key as a migration trigger. Its other markers are
  unchanged.
- **Completing a todo.** On a role_v1 goal that has registered roles,
  `loopx todo complete` of a todo that requires acceptance (see the
  `in_review` status above) delivers it to `in_review` instead of marking it
  done. The event log gains `todo_in_review` and `todo_reopened`.
- **Planning obligations.** role_v1 goals no longer derive these upstream
  planning obligations:
  - the vision checkpoint, and the replan for a todo done with no follow-up
    (decision 31, #18);
  - the projection repair for an idle orchestrator, and the periodic-review
    replan (decision 39, #21);
  - a demand from a stale executable Next Action (decision 42, #25).

  Planning review is the orchestrator's job on every Turn instead.
- **Dependency release.** On role_v1 goals, a dependency that requires
  acceptance releases its dependents only once it is accepted (for a todo
  with repos, accepted and merged). A manual `done` or a supersede does not
  count. A dependency without
  acceptance is still satisfied by `done`.
- **Who may write what.** Under role_v1, agents with the developer or
  acceptor role cannot open user gates or write acceptance criteria. After
  planning, a change to a todo's criteria needs a user-approved plan card.
  Only the owner, using no `--agent-id`, may still edit criteria directly.
- **Agent Turns cannot decide user gates.** Every Turn host sets
  `LOOPX_AGENT_TURN=<agent id>` in the environment of the model process it
  starts. With it set, `loopx gate resolve` and
  `loopx todo complete --role user --decision-outcome` refuse to record a
  decision on a user gate, with `gate_decision_refused_in_agent_turn`. The
  orchestrator replies with `loopx gate reply` instead. This guards against
  accidental self-approval and is not a security boundary: an agent that
  unsets the variable bypasses it. `issue-fix pr-gate-reconcile --execute`,
  which closes the gate of a merged or closed PR as `system`, is refused too
  when it runs inside an agent Turn and would close a gate.
- **Gate decisions are audited.** A user-gate decision records `closed_by`
  in the gate index, with the surface (`cli`, `dashboard` or `system`), the
  actor, the agent Turn if any, and the time. `loopx gate show` shows it. It
  is written after the decision itself; if the process dies in between, a
  replay of the same decision writes it, marked `replayed: true`.
- **The recorded gate decision wins.** Settlement always runs on the
  decision recorded on the gate, never on the caller's. A
  `todo complete --decision-outcome` replay that names another decision is
  refused with `gate_already_decided` and writes nothing. A replay of the
  recorded decision is idempotent. `gate resolve` on a closed gate still fails
  with `gate_closed`.
- **Turn prompts.** Both built-in hosts now ask for repo-relative paths in
  results, and carry role guidance, acceptance criteria and review feedback.
- **codex-cli errors (N9, #24).** An upstream 5xx, `auth_unavailable`, a
  refused connection or a failed upstream dial is now a retryable
  `provider_capacity` error, so the provider backs off, not the todo. A failed
  Turn also keeps a redacted stderr tail.
- **Verdict feedback (N6, #24).** The summary of a review Turn may be up to
  2,000 characters. `review_feedback` keeps its 600-character contract, and
  the text is fitted with the failed criteria first.

### Fixed

- **Pilot v0 fixes (#10).**
  - The orchestrator's LoopX commands are pinned, and its plan todo is
    validated.
  - A gate that awaits the orchestrator no longer blocks its lane.
  - Validation of a `task_repositories` todo runs in the todo's workspace,
    and fails closed when that workspace is missing.
  - The resolved acceptor can start a delivered review.
  - A rejected delivery's feedback reaches its developer.
  - Accept of a todo with repos merges before it completes the todo.
  - The dispatcher never launches an orchestrator Turn that has no todo.
- **Settlement retries (#17).** A Turn keeps its identity when its settlement
  retries run out, and resumes from its workspace only while that workspace
  still exists.
- **Pilot v1 regressions (#23).**
  - A deferred todo is never launched, and no workspace is cut for it.
  - An orchestrator action todo whose Turns keep failing is retired.
  - The action and budget counters survive a dispatcher restart.
- **Pilot v1 gaps (#24).**
  - N3: the status `todo_index` keeps the goal-state status, and superseded
    cards leave the board.
  - N5: accepted todos are counted by their accept record, and the
    orchestrator's spend is split out.
  - N8: the plan gate text refreshes on `--revise`.
  - N10: an untouched todo branch is fast-forwarded when the todo is
    released.
  - N11, in part: the orchestrator can supersede without the claim owner's
    `--agent-id`.
- **UTF-8.** Git subprocess output is decoded as UTF-8 (#5), and so is the
  keychain launch-env subprocess (#3).
- **Hermetic tests (#29).** Multi-agent delivery tests settle from a private
  linked worktree, so they pass from any checkout.
- **Tests no longer touch the real runtime root.** 23 test modules read or
  wrote `~/.codex/loopx`, leaving goals such as `example-goal` and a
  `repository-change-window/` directory behind. They now use temporary
  runtime roots. A test-session guard refuses audited file operations (open,
  mkdir, remove, rename, listdir and similar) under the real runtime root and
  names the offending test. It covers the test process and Python
  subprocesses that inherit its `PYTHONPATH` and guard environment. It does
  not see stat, lstat, access or readlink calls (so not `Path.exists()`),
  `dir_fd`-relative paths, access through an unpinned symlink alias, Python
  subprocesses that drop that environment, or non-Python subprocesses.
- **launchd dispatcher code.** `loopx dispatch launchd-plist` set only
  `PATH` and `HOME`. With the checkout's `.venv` as the Python, a resident
  dispatcher therefore ran the checkout's current code instead of the
  installed snapshot. The plist now pins `PYTHONPATH` to the LoopX source
  that rendered it. Render the plist again after an upgrade.
- **Owner writes on unclaimed todos.** A plan todo without a `bound_agent`
  has no claim owner, because Turns do not claim todos. Two owner actions on
  such a todo failed, because the owner is not a registered agent:
  - the `accept_manually` option of an `acceptor_blocked` gate. It merged
    the delivery and closed the gate, then stopped with
    `agent_id='owner' is not registered` and left the todo `in_review`;
  - `loopx todo supersede --by` without `--agent-id`.

  An owner write is now attributed to the todo's claim owner, else a
  registered fallback (the blocked acceptor, or the agent that proposed the
  plan), else the orchestrator. Both actions now complete.
- **Push gate and deferred todos.** The automatic `push_request` gate
  counted `open`, `in_review` and `blocked` todos as pending work, but not
  `deferred` ones. A goal whose plan dependents were not released yet could
  therefore be offered for push while that work still waited. A deferred todo
  now keeps the automatic gate closed, as it already did for the
  `goal_complete` gate. An explicit `loopx goal request-push` is unchanged.
- **Plan apply before approval.** `loopx plan apply` applied a plan whose
  `plan_approval` gate was still open. It also applied a plan whose gate was
  recorded `reject`, when the card was not settled yet. A pending or
  applying plan now applies only when its gate is recorded done with the
  decision `approve`. Otherwise the apply fails with `plan_not_approved` and
  writes nothing. A plan that was already rejected or cancelled still fails
  with `plan_not_applicable`. Recovering an interrupted apply is unchanged.
- **Dashboard and CLI deciding the same gate.** A gate can now be decided
  from the dashboard and the CLI in any order:
  - A dashboard decision whose gate another surface closed first is reported
    `stale` and changes nothing, even when the decision matches. Before, it
    could report an option you did not choose.
  - The typed gates (`budget_exhausted`, `goal_complete` and
    `acceptor_blocked`) settle one at a time per gate. Before the effect
    runs, the settlement records the chosen option and its inputs, such as
    the note or the budget raise target. Once that record exists, a retry
    after a crash repeats the first choice with the same inputs, and a
    default budget raise is never applied twice.
  - A settlement that did not finish is no longer reported as applied. The
    dashboard marks the decision `failed` with a typed code, and each code
    says how to recover:
    - `gate_settlement_retry_required`: a failed effect, a failed write or a
      lock timeout. Retry the decision.
    - `plan_apply_recovery_required`: an interrupted plan apply. Retry the
      decision, or run the `loopx plan apply` command it names.
    - `gate_push_failed`: a failed push. The failure is final for that gate;
      approve the follow-up push gate that LoopX opens instead.

### Known limitations

- **`hard_lease` goals.** `in_review` is not supported on canonical
  `hard_lease` goals (decision 33). The default `soft_claim` works.
- **Event-projected todos.** `todo update` does not support event-projected
  todos, which are neither Markdown nor canonical. Delivery and verdicts
  therefore need a Markdown or promoted canonical goal.
- **Role board.** On canonical-authority goals, `running` reflects dispatcher
  Turns only. The todo list inherits the status `todo_index` cap of 240
  items across all goals. Verdict text is not shown on the board.
- **codex-cli orchestrators.** The orchestrator's system-prompt addendum and
  state digest only reach claude-code agents. A codex-cli orchestrator gets
  neither.
- **PyYAML.** PyYAML ships only in the `test` extra, and agent and provider
  config files need it. Install with `uv sync --extra test`, or run
  `pip install pyyaml`.
- **Dashboard from source.** A source checkout without the local install
  needs `npm run build:chat` before `loopx dashboard` can serve the chat
  bundle. `scripts/install-local.sh` builds it into the release.
- **Budgets.** A budget pause never kills running Turns, so spend can end up
  above the budget.
- **Usage recording.** The generic-cli and dsh hosts record no usage. A
  codex-cli Turn that times out before `turn.completed` records its duration
  only.
- **Gate authority.** Agent Turns cannot decide user gates, but this is a
  guardrail, not a security boundary: agents run as your OS user, and an
  agent that unsets `LOOPX_AGENT_TURN` can still decide a gate. `closed_by`
  records who decided each gate. A DSH Turn run through an explicit
  `--dsh-runner` runs in process and does not carry the marker.
- **CLI crash while closing a typed gate.** The CLI closes the gate before
  its settlement records the chosen option. If the process dies between the
  two, the option is not recorded yet, and a retry settles with the option
  that the retry passes.
- **Typed gates with unreadable options.** When the dashboard cannot read a
  typed gate's option list, it disables that gate's decisions. Decide it with
  `loopx gate resolve --option ...` instead.
- **Orchestrator ergonomics.** The orchestrator cannot run
  `todo add --agent-id` for agent todos (N11), so it omits `--agent-id`. Its
  clarification questions may arrive in the plan gate thread instead of a
  separate question gate (N7).
- **Not built yet.**
  - remote-SSH workspaces for AOSP-scale trees;
  - a leasable resource pool;
  - Lark and Telegram channels;
  - the dashboard intake form;
  - accept and reject actions on role board cards.

  Several design docs under `docs/fork/` list further known gaps.

### Documentation

- [docs/fork/usage.md](docs/fork/usage.md): a new usage guide for the product
  workflow and the CLI.
- Fork design and protocol docs:
  - [design-v0](docs/fork/design-v0.md), which records decisions 1 to 43;
  - [role-v1-protocol](docs/fork/role-v1-protocol.md);
  - [dispatcher-v0](docs/fork/dispatcher-v0.md);
  - [gates-plans-intake-v0](docs/fork/gates-plans-intake-v0.md);
  - [workspaces-v0](docs/fork/workspaces-v0.md);
  - [agent-and-provider-config](docs/fork/agent-and-provider-config.md);
  - [usage-accounting-v0](docs/fork/usage-accounting-v0.md);
  - [role-board-v0](docs/fork/role-board-v0.md).
- End-to-end pilot reports: [v0](docs/fork/e2e-pilot-report-v0.md) and
  [v1](docs/fork/e2e-pilot-report-v1.md).
