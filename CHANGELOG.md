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

Covers 2026-09-25 to 2026-09-28: PRs #1 to #29, 136 commits.

### Highlights

- **The orchestrator role.** In upstream `peer_v1`, every worker agent plans,
  decomposes and claims work for itself, and a hash picks who owns a replan.
  The fork adds the `role_v1` runtime model, which has three registered roles:
  - one **orchestrator** per goal. It clarifies requirements with you, plans,
    owns the acceptance criteria, answers escalations, and is the only agent
    that opens user gates;
  - **developers**, which implement todos;
  - **acceptors**, which only review.

  `role_v1` is the default for new goals.
- **A resident dispatcher.** `loopx dispatch serve` launches role Turns when
  state changes and on a periodic tick. It only schedules: every write still
  goes through the existing CLI and kernel contracts.
- **You talk to the orchestrator through gates.** Every user gate has a
  discussion thread. The initial plan and every major change arrive as a
  plan card that you approve. `loopx goal create` turns a requirements
  document into a running goal.
- **Review before merge.** A developer's delivery moves to the new `in_review`
  status. An isolated acceptor then accepts, rejects or blocks it. Accepted
  work merges atomically across repos. The second rejection of a todo
  escalates it to the orchestrator.
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
  `acceptance_criteria`. `loopx todo add` and `loopx todo update` take the
  matching flags: `--required-role`, `--requires-acceptance`,
  `--acceptor-agent`, `--reject-count`, `--task-repo`,
  `--acceptance-criteria` and `--review-feedback`, plus their `--clear-*`
  forms.
- **Role-aware selection.** An agent only receives todos whose effective role
  matches its own. User gates, blockers and planning work belong to the
  orchestrator, so it never receives implementation work. An acceptor only
  receives the `in_review` todos it is the resolved acceptor for.
- **Replan routing.** Replan obligations go to the goal's orchestrator
  instead of the owner that the sha256 peer hash picks.
- **Only the orchestrator opens user gates (#7).** Developers and acceptors
  raise `blocker` todos. The orchestrator answers them itself, or turns them
  into a gate with options and a recommendation.
- **Less orchestrator overhead (decision 43, #27).** LoopX now completes pure
  bookkeeping without a model Turn: the planning todo once its plan is
  applied, an escalation whose todo is already done, and a gate-reply action
  todo whose gates were already answered. Every orchestrator Turn also starts
  from a bounded goal state digest (at most 20,000 characters), so it needs
  fewer CLI calls to discover state.

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
  - On approval, LoopX creates all the plan's todos at once. Dependent todos
    start `deferred`.
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
- **Web discussion panel.** The gate drawer lists the thread and has a reply
  box. Two loopback-only endpoints back it: `GET /api/chat/gate-thread` and
  `POST /api/chat/gate-thread/reply`.

#### Acceptance flow (#8, #11, #13, #16)

- **The `in_review` status.** On a role_v1 goal that has an acceptor,
  completing a developer todo does not mark it done. LoopX first runs the
  todo's declared validation command, then moves the todo to `in_review`. It
  records who delivered it and the commit it delivered in each repo
  (`delivered_shas`).
- **Verdicts.** `loopx todo accept | reject | block-review`. An acceptor's
  Turn result maps to the same verdicts:

  | Turn result | verdict |
  |---|---|
  | `validated_completion` | accept |
  | `repair_required` with a summary | reject |
  | `user_action_required` | blocked |

  - Accept merges the todo first and completes it only after the merge.
  - Reject reopens the todo for the same developer, with feedback that names
    each failed criterion.
  - The second rejection blocks the todo and opens an escalation todo for
    the orchestrator.
- **Per-todo acceptance criteria (G2, #11).** The criteria live in a field
  that only the orchestrator writes, and developer and acceptor Turns see
  them every time. Rework instructions go to `review_feedback`.
- **Acceptor isolation (G12, #13).**
  - The acceptor reviews in a throwaway detached checkout of the delivered
    commit, never in the developer's worktree.
  - The merge takes exactly the delivered commit. A branch that moved after
    delivery blocks the merge.
  - Changes the acceptor makes to its checkout are detected, recorded and
    discarded.
  - By default the acceptor runs without a sandbox, so it can build and test.
- **The `acceptor_blocked` gate.** When the acceptor cannot review, for
  example because tooling is broken, you resolve the gate with one option:
  `retry_acceptance`, `accept_manually`, `return_to_developer` or
  `cancel_todo`.
- **Dependency release and supersession (decision 37, #16).**
  - On a role_v1 goal, a dependency that requires acceptance releases its
    dependents only once it is accepted and merged.
  - To replace or split a todo, run
    `loopx todo supersede --goal-id G --todo-id OLD --by NEW[,NEW2]`. This rewires the
    dependents, and a superseded todo never counts as done.
  - `loopx todo list` explains why a deferred todo still waits
    (`dependency_waits`).

#### Git workspaces, merge and push (#1, #12, #19, #25)

- **Goal repos.** Declare a goal's repos with `loopx configure-goal --repo
  NAME=PATH[,default_branch=B][,merge_target=main|task_branch][,task_branch=B]`.
  A goal without a repo list keeps its legacy single `repo`, which acts as the
  repo `main`.
- **Workspaces.** `loopx workspace prepare | status | merge | cleanup`. A
  todo gets one worktree per repo, all on the branch `loopx/<goal>/<todo>`.
  - The merge is atomic across repos: if any repo fails, none is merged.
  - Each repo gets a no-ff merge commit with `LoopX-Goal` and `LoopX-Todo`
    trailers.
  - The default task branch is `loopx-task/<goal>`.
  - The dispatcher prepares workspaces itself.
- **Delivery identity (decision 29, #12).** A multi-repo Turn settles against
  a todo workspace identity. A repo without `origin` gets a local `repo_id`,
  so it works without a fake remote.
- **Push gate (decision 38, #19).** Once all of a goal's work is merged, one
  `push_request` gate lists the unpushed commits of each repo.
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
  `api_key`, `oauth_cli` or `oauth_token`. A provider names an env var or a
  keychain entry, and LoopX refuses secret values in the file.
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
  block with tokens, cost, host steps and duration. The rows go to an
  append-only `<runtime-root>/goals/<goal>/usage.jsonl`.
- **Estimated pricing.** A provider can declare a `pricing` table, which
  estimates the cost of hosts that report no USD, such as Codex through CPA.
- **Reports.** `loopx usage report [--by role|agent|goal|todo|model|day]` shows
  agent-hours, cost and Turns. It also shows the cost per accepted todo, with
  and without the orchestrator's share. The status projection carries the same
  numbers as `turn_usage_summary`.
- **Budgets (decision 41, #22).** Budgets are optional:
  `loopx usage budget --goal G --set USD`.
  - At 80% of the budget, LoopX opens a non-blocking alert.
  - At 100%, a `budget_exhausted` gate pauses the goal's new Turns, and
    running Turns finish. Resolve it with `raise_budget`,
    `continue_without_limit` or `stop_goal`.

#### Dashboard (#9 and later)

- **The role board tab (角色看板).** It opens with a "Waiting on you" list of
  gates and plan approvals. Below that, columns Planned, Assigned, Rework,
  Running, In review and Done are split into orchestrator, developer and
  acceptor swimlanes.
- **Cards.** A card shows its agent, repos, reject count, acceptor,
  acceptance criteria, why it waits on dependencies, and its plan link.
- **The usage strip.** It shows cost, agent-hours, cost per accepted todo and
  the budget share.
- **The gate drawer.** It shows the discussion thread and reply box, plus the
  typed options of `acceptor_blocked`, `budget_exhausted` and `goal_complete`
  gates. Criteria changes appear old and new side by side.

### Changed

These behaviors differ from upstream.

- **Runtime model.** New goals, and goals that record no model, run
  `role_v1`. Goals that already record `peer_v1` keep the flat peer behavior
  until you run `loopx configure-goal --agent-model role_v1`.
- **Anti-hierarchy validators removed.** `agent_profiles.*.profile_role`
  accepts labels such as `orchestrator`, `manager` and `worker`. The legacy
  hierarchy detector no longer fires on role_v1 goals.
- **Completing a todo.** On a role_v1 goal with an acceptor, `loopx todo
  complete` of a developer todo delivers it to `in_review` instead of marking
  it done. The event log gains `todo_in_review` and `todo_reopened`.
- **Planning obligations.** role_v1 goals no longer derive these upstream
  planning obligations:
  - the vision checkpoint, and the replan for a todo done with no follow-up
    (decision 31, #18);
  - the projection repair for an idle orchestrator, and the periodic-review
    replan (decision 39, #21);
  - a demand from a stale executable Next Action (decision 42, #25).

  Planning review is the orchestrator's job on every Turn instead.
- **Dependency release.** On role_v1 goals, only an accept and merge releases
  a dependent. A manual `done` or a supersede does not.
- **Who may write what.** Under role_v1, developers and acceptors cannot open
  user gates or write acceptance criteria. After planning, a change to a
  todo's criteria needs a user-approved plan card. Only the owner, using no
  `--agent-id`, may still edit criteria directly.
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
  - Accept merges before it completes the todo.
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

### Known limitations

- **`hard_lease` goals.** `in_review` is not supported on canonical
  `hard_lease` goals (decision 33). The default `soft_claim` works.
- **codex-cli orchestrators.** The orchestrator's system-prompt addendum and
  state digest only reach claude-code agents. A codex-cli orchestrator gets
  neither.
- **PyYAML.** PyYAML ships only in the `test` extra, and agent and provider
  config files need it. Install with `uv sync --extra test`, or run
  `pip install pyyaml`.
- **Dashboard from source.** A source checkout needs `npm run build:chat`
  before `loopx dashboard` can serve the chat bundle.
- **Budgets.** A budget pause never kills running Turns, so spend can end up
  above the budget.
- **Manual accept of an unclaimed todo.** When a delivered todo has no claim
  owner, the `accept_manually` option of an `acceptor_blocked` gate applies
  only in part. The gate closes and the merge lands, but the todo stays
  `in_review` with the error `agent_id='owner' is not registered`. To finish
  it, run the accept as the acceptor:
  `loopx todo accept --goal-id G --todo-id T --agent-id <acceptor>`. A todo
  that has a claim owner is not affected.
- **Usage recording.** The generic-cli and dsh hosts record no usage. A
  codex-cli Turn that times out before `turn.completed` records its duration
  only.
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
