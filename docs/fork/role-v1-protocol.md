# role_v1 agent runtime (fork slice S1)

Status: implemented in slice S1 (roles in the kernel). It replaces the flat
`peer_v1` model for new goals. See [design-v0](design-v0.md), decisions 1, 5, 8, 11, 20 and 22.

## Model selection

- `coordination.agent_model` is `role_v1` or `peer_v1`.
- A goal without a recorded model is treated as `role_v1`. `configure-goal`
  and `register-agent` stamp `role_v1` on such goals when they write the
  coordination block.
- Goals that already record `peer_v1` stay on `peer_v1` until you run
  `loopx configure-goal --agent-model role_v1`. peer_v1 code paths are kept
  and removed only when they get in the way (decision 20).

## Roles in the registry

`coordination.agent_roles` maps a registered agent id to `orchestrator`,
`developer` or `acceptor`.

The following commands write it:

```bash
loopx register-agent --goal-id G --agent-id fable-orch --role orchestrator --execute
loopx configure-goal --goal-id G --agent-role opus-dev=developer \
  --agent-role codex-acc=acceptor --execute
loopx configure-goal --goal-id G --clear-agent-role opus-dev --execute
```

The following rules apply:

- A role key must name a registered agent. When you unregister an agent, its
  role is dropped.
- A goal may have **at most one orchestrator**. The rule is checked on the
  merged map, so to move the role you clear the old orchestrator and assign
  the new one in the same call.
- The quota identity packet (`peer_agent_identity_v1`) carries `role` for the
  requesting agent and `orchestrator_agent_id` for the goal. Both are present
  only under role_v1.

## Removed anti-hierarchy rules

- `agent_profiles.*.profile_role` is now a bounded, public-safe advisory label.
  Names such as `orchestrator`, `manager` and `worker` are allowed.
- For role_v1 goals, the legacy (v0.1) hierarchy detector no longer treats
  an `agent_profiles.*.role` key as a migration trigger. The other legacy
  markers (`primary_agent`, `side_agent_handoff_agent`, `agent_profile_v0`)
  are unchanged.

## Todo contract fields

The following fields were added to `coordination_state_contract_v0.json`
(`todo_read_record`) and to the Markdown metadata codec:

| field | type | default / notes |
|---|---|---|
| `required_role` | `orchestrator\|developer\|acceptor` | optional; see routing below |
| `requires_acceptance` | bool | derived when absent: true for developer advancement todos, otherwise false (`todo_requires_acceptance`) |
| `acceptor_agent` | registered agent id | optional |
| `reject_count` | int | 0 is the implicit default and is not stored |
| `task_repositories` | list of Goal repo names | up to 8 names; each must be a repo the Goal declares (`configure-goal --repo`, S5), or the legacy `main` repo. This field coexists with the singular `task_repository` Git identity |

CLI flags (todo `add` and `update` only): `--required-role`,
`--clear-required-role`, `--requires-acceptance true|false`,
`--acceptor-agent`, `--clear-acceptor-agent`, `--reject-count N`,
`--task-repo NAME` (repeatable), `--clear-task-repos` and
`--acceptance-criteria TEXT` (G2). `todo update` also takes
`--clear-acceptance-criteria`, `--review-feedback TEXT` and
`--clear-review-feedback`.

The Python API takes one `role_contract` patch. In that patch, present keys
are written and `None` clears a field. The same patch reaches the TypeScript
field planner (`role_contract` intent), so the Markdown and canonical
authority paths use a single codec.

Slice S2 adds two more role-contract fields, written by the acceptance
flow rather than by hand: `delivered_by` (the developer who delivered the
todo for review) and `review_feedback` (the acceptor's latest verdict text,
at most 600 characters). `todo update --review-feedback TEXT` (and
`--clear-review-feedback`) also writes it: the orchestrator's rework
instructions go there, never into the note (E2E pilot gap G10).

Gap G2 adds `acceptance_criteria`, the orchestrator-owned per-todo
acceptance criteria. See
[Per-todo acceptance criteria](#per-todo-acceptance-criteria-gap-g2).

## Role-aware selection

`todo.quota_planning.project` (request schema
`todo_quota_planning_request_v2`) takes `selection.agent_model` and
`selection.agent_role`. Under role_v1, an agent that has a role only
receives todos whose effective role matches its own role.

The effective role of a todo is determined as follows:

1. An explicit `required_role` wins.
2. User gates and user actions, blockers, and planning work belong to the
   orchestrator. Planning work is a todo with a `replan_obligation_id` or an
   `action_kind` in `plan|replan|decompose|triage`.
3. Everything else defaults to `developer`.

As a result, the orchestrator never receives developer implementation work,
and an acceptor only receives work that is explicitly tagged
`required_role=acceptor`. The lane diagnostic `role_scope` reports the
filtered count.

The following behaviour is unchanged:

- Agents without a registered role keep the flat peer behaviour.
- `peer_v1` goals are never role-filtered.
- Explicit User gate *blocking* scope (`user_gate_scope`) is unchanged.

## Turn lanes (decision 32)

`turn run-once` admits one executing Turn per lane. Under role_v1 the lane of
a registered developer or acceptor Turn with a selected todo is the todo:
(goal, todo). One agent may run several todos of a goal at once (the
dispatcher bounds it by `max_concurrency` and the machine cap), while any one
todo never has more than one executing Turn, whichever agent runs it. A second
Turn on the same todo is refused with `turn_lane_in_flight` and
`turn_lane: {scope: todo, todo_id}`.

The orchestrator stays strictly serial per goal (decision 19): its lane is
(agent, goal). peer_v1 goals, agents without a registered role and Turns
without a selected todo keep the (agent, goal) lane unchanged
(`turn_lane: {scope: agent}`).

Claims are unaffected: a soft claim is per todo, and nothing limits an agent
to one claim per goal, so a developer holding two claimed todos of one goal
is already valid. Canonical `hard_lease` goals are out of scope; their lease
semantics are unchanged.

## Replan routing

When a role_v1 goal has a registered orchestrator:

- `autonomous_replan_scope_decision` routes every required replan obligation
  to the orchestrator (`scope=role_v1_orchestrator`). This includes
  evidence-authored and explicitly owned obligations. It replaces the
  sha256 peer hash.
- `select_autonomous_replan_obligation` lets the orchestrator pick up an
  obligation raised in another agent's lane. It annotates the obligation with
  `routed_agent_id` and `evidence_agent_id` and leaves the obligation
  identity unchanged, so obligation ids, successor bindings and ACKs stay
  stable. Semantic replan writeback uses the same per-lane derivation.
- The adaptive task-orchestration coordinator is the orchestrator.
- The goal-amendment open-obligation inventory binds obligations to the
  orchestrator.

When no orchestrator is registered, or the goal is `peer_v1`, the previous
rules apply unchanged.

## Planning obligations (decision 31)

A role_v1 goal does not raise two upstream planning obligations. Planning
review is the orchestrator's job on every Turn, and developers and acceptors
escalate through orchestrator todos. This applies to every role and to every
goal whose runtime model is role_v1, including goals that record no model.

- **Vision checkpoint.** A material closeout (`refresh-state`, or the
  settlement of a Turn) owes no per-agent vision decision. The vision
  checkpoint is recorded with `decision=not_required` and
  `policy=not_required`, never `missing_required`. A supplied vision patch is
  still recorded.
- **Vision gaps.** The goal frontier derives no per-agent vision gaps:
  `vision_checkpoint_missing`, `vision_outcome_checkpoint_required`,
  `vision_acceptance_gap` and `required_agent_vision_missing`.
- **No-follow-up replan.** A completed advancement todo without a successor or
  a no-follow-up rationale (`completed_advancement_without_successor`) no
  longer becomes a required replan. The `todo_succession_warning` diagnostic
  stays in the todo summary.

Both obligations are derived from state on every read, not stored. A role_v1
goal that already carries one, for example a missing checkpoint that a pilot
Turn recorded, stops deriving it, so it is no longer blocked.

**Turn contract.** `turn run-once` marks a role_v1 Turn plan with
`vision_checkpoint_policy=not_required`. The shared Turn prompt then tells
the host to leave `path_delta_mode`, `agent_vision_json` and
`vision_unchanged_reason` empty. The result validator ignores those fields
instead of validating them, so an invalid vision packet no longer fails the
Turn (E2E pilot gap G4).

**Unchanged.** peer_v1 goals raise both obligations as before. Stall replan
obligations from run history (repeated typed progress, dead monitors, a
blocked successor without progress, a monitor no-change streak) are still
derived and still routed to the orchestrator. The periodic review is not, see
the next section.

## Idle orchestrator (decisions 39 and 42)

Under role_v1 an idle orchestrator is the normal state: it is event-triggered
through its todos. Its Next Action typically reads "Orchestrator idles. Next
eligible work is acc reviewing todo_X. Orchestrator re-engages only if acc
rejects it or a user gate appears." Three upstream checks read that state as a
demand on the orchestrator. A role_v1 goal raises none of them (E2E pilot gap
G13, decision 39; pilot v1 gap N2, decision 42):

- **Self-reported wait.** The state-projection check records a Next Action
  that reads as a wait on the user/owner/gate without an open User Todo as
  `next_action_waits_without_user_todo`, and should-run then demanded
  `effective_action=state_projection_gap_repair` from every agent. should-run
  re-derives that demand on read (`build_state_projection_gap`), and for a
  role_v1 goal it drops the wait evidence, including from a gap persisted
  before this change. The evidence is still recorded by `refresh-state` and
  still shown by `status` and `contract check` as a diagnostic.
- **Stale executable Next Action (decision 42).** The other half of the same
  check, `next_action_executable_without_agent_todo`: an executable-sounding
  Next Action with no open agent todo (`in_review` and `blocked` count as
  open). Under role_v1 the goal's Next Action is written by whichever Turn
  settled last, typically the acceptor ("Settle todo_X as accepted; no
  developer repair is required."), so it is no orchestrator obligation. In the
  pilot v1 it fired on the finished goal and cost an action todo, a Fable Turn
  and a closure gate that repeated the push approval (N2). should-run drops
  that evidence for a role_v1 goal the same way
  (`next_action_executable_demands_agent_todo`), so no role gets a projection
  repair, replan or orchestrator action from a Next Action. The end of the
  work is marked by the dispatcher's deterministic goal_complete gate instead
  (see [gates-plans-intake-v0](gates-plans-intake-v0.md#goal-complete-gate-decision-42)):
  the orchestrator never opens a closure gate itself.
- **Periodic review.** The run-history cadence trigger `periodic_review_due`
  (a lane recorded 20 durable runs since the last replan ACK, whether it
  progressed or not) was routed to the orchestrator as
  `autonomous_replan_required`. The goal frontier selects the replan
  obligation from sources without cadence-only obligations
  (`goal_frontier/role_v1_cadence.py`), so the refresh-state replan gate
  follows too.

Real stuck work still wakes the orchestrator through the dispatcher: a gate
reply awaiting it, an S2 escalation todo, and a stall replan from run history
(see [dispatcher-v0](dispatcher-v0.md)), and user follow-up work reaches it
from the goal_complete gate as an orchestrator todo. peer_v1 goals keep all
three checks.

Surveyed and unchanged, because they did not fire on the pilot state and a
realistic role_v1 flow gives them real work:

- The stall triggers above, and `successor_replan_required` from a cleared
  handoff gate without a successor (`agent_scope._cleared_handoff_frontier`).
- `waiting_without_owner_projection` (`stall_repair.py`): opt-in through
  `control_plane.self_repair.enabled`, off by default.

## Orchestrator bookkeeping and state digest (decision 43)

Two cost rules for the orchestrator, from the E2E pilot v1 usage ledger:

- **Bookkeeping is done by LoopX.** An orchestrator todo whose outcome LoopX
  can decide from state is completed without a model Turn, attributed to the
  orchestrator and with the evidence the Turn would have reported: the
  planning todo once its plan card is applied (at apply time), an escalation
  whose escalated todo is already done, and a gate-reply action todo whose
  gates were already answered or settled by LoopX. Judgment stays a Turn:
  planning, clarification, answering a user's gate message, deciding a
  still-blocked escalation, criteria-change outcomes, replans and user
  follow-ups. See [dispatcher-v0](dispatcher-v0.md) (step 4, mechanical
  bookkeeping).
- **The orchestrator starts from a digest.** Every orchestrator Turn's
  system-prompt addendum carries a bounded goal state digest rendered at
  launch (why the Turn runs, the goal and its contract, repos, open gates with
  thread tails, pending plans, todos with criteria, waits, feedback and
  delivery state, recent events; at most 20,000 characters). The orchestrator
  uses CLI reads only for detail the digest omits and still writes only
  through the CLI; every write is validated against the current state, so the
  digest is advisory. See
  [dispatcher-v0](dispatcher-v0.md#orchestrator-state-digest-decision-43).

Developer and acceptor Turns are unchanged. peer_v1 goals are unaffected.

## Acceptance flow (slice S2)

See [design-v0](design-v0.md), decisions 5 to 9.

### The `in_review` status

`in_review` is a first-class todo status next to `open`, `blocked`,
`deferred` and `done`. It is non-terminal (`done=false`, Markdown marker
`[ ]`), applies only to agent todos, and cannot be created with `todo add`.
Developers never receive it as executable work.

### Delivery

Under role_v1, `loopx todo complete` (and the managed Turn writeback of
`loopx turn run-once`) on an open agent todo whose effective
`requires_acceptance` is true does not mark the todo done. Instead:

1. The todo's declared validation command runs, exactly as for an ordinary
   completion. A failing command returns `validation_blocked_completion` and
   the todo stays `open`.
2. The todo moves to `in_review`. The claim is kept. `delivered_by` records
   the developer. `evidence` records the delivery: `delivered_by`, the
   shared branch and repos for multi-repo todos (S5), the delivered commit per
   repo (`delivered_shas=<repo>@<sha>,...`, G12), the workspace identity
   kind, the validation result and the developer's evidence text.

The built-in `claude-code` and `codex` Turn hosts are offered the
`validated_completion` result kind when the Turn selected a todo. The Turn
validator (the dispatcher passes the todo's own validation command) gates
that result. The todo lifecycle then runs the todo's declared validation
again, in the Goal repository or in the verified delivery worktree of the
todo's `task_repository`, and completes the todo or delivers it for review.
A delivered (`in_review`) todo no longer counts as pending controller
validation, so the Turn's refresh writeback is not blocked.

Successor and no-follow-up options are not applied on delivery. Follow-up
work is planned by the orchestrator after the verdict. Todos with
`requires_acceptance=false` go straight to `done`. A second `todo complete`
on an `in_review` todo fails, except when a retried Turn settlement replays
its own delivery.

### Verdict

```bash
loopx todo accept --goal-id G --todo-id T --agent-id codex-acc [--note ...] [--evidence ...]
loopx todo reject --goal-id G --todo-id T --agent-id codex-acc --note "criterion 2 fails: ..."
loopx todo block-review --goal-id G --todo-id T --agent-id codex-acc --reason "why I cannot review"
```

The Python API is `loopx.todo_acceptance.accept_goal_todo` and
`reject_goal_todo`, and `loopx.todo_review_blocked.block_goal_todo_review`
(G12, see [Acceptor verdicts and isolation](#acceptor-verdicts-and-isolation-gap-g12)).

Only the resolved acceptor can record a verdict. The resolved acceptor is
the todo's `acceptor_agent`. If none is bound, it is the goal's single
`role=acceptor` agent. If there are zero or several acceptors and none is
bound, the verdict is refused and the review is routed to the orchestrator,
which binds one with `todo update --acceptor-agent`.

- **Accept** runs the ordinary completion transaction (the validation
  command runs again) and the todo becomes `done`. The completion evidence
  starts with `accepted_by=<acceptor>`. For a todo with an S5 workspace,
  accept first runs the atomic merge into the goal's merge target, and only
  then completes the todo, so a todo is never `done` without its merge. The
  merge merges exactly the delivered sha recorded at delivery (G12). A
  blocked or failed merge reopens the todo for its developer with the
  conflict report in `review_feedback` (transition `merge_blocked`,
  reject_count unchanged); a todo branch that moved after delivery blocks
  with reason `delivery_moved`. If the merge lands but the completion is refused
  (the re-run validation fails), the todo stays `in_review` (transition
  `completion_blocked`, the merge is in the result). The merge is
  idempotent, so the acceptor can retry the accept, or reject. Plan
  dependents whose dependencies are now all done are reopened only after a
  completed accept.
- **Reject** reopens the todo (`open`) for the same developer. The claim is
  kept, `reject_count` is incremented, and `review_feedback` stores the
  feedback. The feedback is required and should name each acceptance
  criterion that failed (prompt guidance, not parsed); an empty note is
  refused.
- **Blocked** (G12) records that the acceptor cannot review for its own
  reasons. The todo stays `in_review`, `reject_count` is unchanged, and a
  system user gate opens. See below. The developer's next selection carries `review_feedback` in the
  selected todo item.
- **The second rejection escalates.** The todo becomes `blocked` with
  reason "escalated to the orchestrator". Its claim is kept and it is never
  auto-reassigned. A new agent todo is created with `required_role` set to
  `orchestrator`, `action_kind=replan` and `requires_acceptance=false`. It is
  claimed by the orchestrator, which decides whether to reassign, split,
  change the criteria or open a user gate. When it reopens the todo, its
  rework instructions go to `review_feedback` (`todo update --review-feedback`),
  not to the note.

**Turn context.** The Turn envelope's selected todo carries `required_role`,
`requires_acceptance`, `acceptor_agent`, `task_repositories`, `reject_count`,
a bounded `review_feedback`, the todo's bounded `note`, its
`acceptance_criteria` and, when the goal has an enabled goal acceptance
contract, a bounded `goal_acceptance` brief (objective and criteria). The
shared Turn prompt spells out the review contract for an `in_review` todo and
the feedback for a reopened one, and shows both sets of criteria. The resolved acceptor may pin a delivered todo
with `turn run-once --todo-id`, and a gate scoped to another agent does not
keep its lane in `operator_gate`. A reject verdict from a Turn settles as
`outcome_progress`.

**Verdicts from a managed Turn.** An acceptor Turn (`loopx turn run-once`,
for example launched by the dispatcher) selects the delivered todo. A
`validated_completion` result is the accept verdict. A `repair_required`
result is the reject verdict, and its summary becomes the feedback; with an
empty or blank summary it is the blocked verdict instead.

**Verdict feedback length (pilot v1 gap N6).** The Turn result `summary` is
bounded at 400 characters, but on a Turn that reviews an `in_review` todo
the bound is 2000 (`ACCEPTOR_VERDICT_SUMMARY_LIMIT`, in both the executor
and the codex/claude result schema), so a host never runs into it while it
names the failed criteria. `review_feedback` keeps its 600-character
contract (the Python codec and the TypeScript field planner share it; raising
it would be a contract change). Every reject, from a Turn or from `todo reject
--note`, is fitted into it (`loopx.control_plane.todos.review_feedback`):
trailing host or terminal glyphs are stripped (control, replacement and
box-drawing characters, and a short CJK or fullwidth run after text that has
none), feedback that fits is kept verbatim, and longer feedback moves the
sentences that name a failed criterion first and is cut once at a word
boundary with `...`, so the whole 600 characters are used. The review prompt
asks the acceptor to put the failed criteria first and stay within 550
characters. A
`user_action_required` result is the blocked verdict, and its summary is the
reason (G12). A host crash or timeout (`iteration_failed`, host failure) is
not a verdict and does not count. A
completion by the resolved acceptor through `loopx todo complete` is also
treated as accept. Required replan obligations belong to the orchestrator
(S1 routing), so they no longer fence the accountable writeback of another
role's Turn.

Lifecycle writes for a verdict are attributed to the todo's claim owner, so
the kernel's claim fence and task leases are unchanged. The acceptor's
authority is checked by the acceptance layer and recorded in the evidence,
in `review_feedback` and in the returned `acceptance` packet.

### Selection

- The identity packet carries `acceptor_agent_ids` under role_v1.
- An `in_review` todo is executable only by its resolved acceptor, even
  though the developer still holds the claim. When no acceptor resolves, it
  goes to the orchestrator. `role_scope.review_open_count` reports it.
- **Role-aware user gates.** A user gate that is not global and not scoped
  to the acceptor (by `blocks_agent` or its claim) no longer blocks the
  acceptor's lane. Global gates still block every lane. Developers and the
  orchestrator keep the previous gate rules.
- **Criteria changes (decision 40).** An in_review todo whose acceptance
  criteria change awaits the user on a plan card is held from its acceptor;
  see [below](#criteria-changes-need-a-plan-card-decision-40).

### Event log

The retained state event log gains `todo_in_review` and `todo_reopened`
events. Replay folds them to `in_review` and `open`, and Markdown backfill
emits `todo_in_review` for todos that are in review.

### When delivery applies

The implicit `requires_acceptance` default (true for developer advancement
work) diverts a completion only on a role_v1 goal that has registered roles
and an acceptor to review it: a bound `acceptor_agent` or at least one
`role=acceptor` agent. An explicit `requires_acceptance=true` diverts as
long as the goal has registered roles. Goals without roles complete todos
directly, as before.

### Known gaps

- `todo update` on event-projected (non-Markdown, non-canonical) todos is
  not supported by the kernel. Delivery and verdicts therefore need
  Markdown or promoted canonical authority.
- Canonical `hard_lease` goals: the kernel changes a leased todo's status
  only through an atomic lifecycle operation, and none exists yet for
  `in_review`. A delivery on such a goal is rejected by the lease fence and
  the todo stays open. The default `soft_claim` mode works, and covers the
  MVP; this stays unsupported for now (decision 33).
- Accept completes directly from `in_review` through the terminal
  transaction, attributed to the claim owner.
- A todo with S5 `task_repositories` validates in its prepared per-todo
  workspace (the worktree for one repo, the workspace root for several), which
  must be clean on the todo branch. The workspace is looked up under the
  caller's `--runtime-root` (the dispatcher's). If it is missing or off the
  todo branch, validation fails closed with `workspace_unverified`; it never
  falls back to the Goal repository, which lacks the todo's commits. Run
  `loopx workspace prepare` again to recreate a removed worktree on the
  existing todo branch.
- A multi-repo Turn runs from the workspace root, which is not a git worktree.
  Its delivery and verdict bind to the todo workspace identity (design
  decision 29): goal, todo, the todo branch and, per repo, its name,
  root-relative path, HEAD and `repo_id`. The post-settlement refresh accepts
  that root as the independent workspace only when it is the registered root
  of exactly this todo and every repo is a worktree of the declared repo on the
  todo branch. The Turn then settles, spends quota and closes its journal. A
  repo without `origin` gets a local `repo_id`, a digest of its git common
  dir. See [workspaces-v0](workspaces-v0.md#delivery-identity-decision-29).

## Acceptor verdicts and isolation (gap G12)

See [design-v0](design-v0.md), decisions 35 and 36.

### The acceptor only reviews

- **Delivery** records the delivered commit per repo for todos with
  `task_repositories`: the tip of `loopx/<goal>/<todo>` goes into the delivery
  evidence as `delivered_shas=<repo>@<sha>,...`. A later delivery records the
  new tip. The decision-29 delivery identity also carries per-repo `head_sha`,
  but only for Turn deliveries from the workspace root; `delivered_shas` covers
  every delivery path, the CLI included.
- **Review checkout.** The dispatcher runs the acceptor's Turn in a throwaway
  detached worktree per repo at the delivered sha,
  `<runtime_root>/goals/<G>/reviews/<T>/<attempt>/<repo>`, never in the
  developer's todo worktree. A detached HEAD moves no branch, so nothing the
  acceptor does there can reach the todo branch. After the Turn settles the
  checkout is removed (best-effort, idempotent). See
  [workspaces-v0](workspaces-v0.md) and [dispatcher-v0](dispatcher-v0.md).
- **Merge.** The accept merge merges exactly the recorded delivered sha. If the
  todo branch moved after delivery, the merge is blocked (`merge_blocked`,
  reason `delivery_moved`) and the todo returns to the developer, who
  delivers again.
- **Detection.** If the review checkout has changes or new commits after the
  Turn, the verdict still stands, the changes are discarded, and the event log
  records `acceptor_modified_review_checkout`. The role board card shows
  `review_checkout_modified`.
- **Sandbox.** The acceptor's role default is unsandboxed: codex
  `danger-full-access`, claude-code `bypassPermissions`. Explicit agent config
  still wins. See [agent-and-provider-config](agent-and-provider-config.md).
- **Prompt.** The acceptor is told it only reviews and delivers a verdict,
  never modifies code, and puts the required changes into the rejection
  feedback.

Todos without `task_repositories` have no per-repo branch, so there is no
review checkout or pinned merge; the acceptor Turn runs in the goal project as
before.

### Three verdicts

| verdict | how | effect |
|---|---|---|
| accept | `todo accept`, Turn `validated_completion` | merge the delivered sha, then complete |
| reject | `todo reject --note`, Turn `repair_required` with a summary | reopen for the developer, `reject_count` + 1; the second escalates |
| blocked | `todo block-review --reason`, Turn `user_action_required`, Turn `repair_required` with a blank summary | stays `in_review`, no count, opens a system user gate |

**Blocked** is for when the acceptor cannot review for its own reasons: broken
tooling, the environment, missing dependencies. LoopX, not the acceptor,
opens one user gate of kind `acceptor_blocked` (the same class of exception
to decision 11 as the dispatcher's re-login gate, decision 17). The gate text
carries the acceptor's reason, and `blocks_agent` is the acceptor, so the gate
holds the acceptor's lane and nobody else's. A second blocked verdict while
the gate is open reuses it. The gate index entry records `review_todo_id`,
`acceptor_agent` and `options`, and the event log gets `todo_review_blocked`.
While the gate is open the dispatcher does not relaunch the acceptor on that
todo.

The owner resolves the gate with one option:

| option | decision | effect |
|---|---|---|
| `retry_acceptance` | approve | the todo stays `in_review` and the acceptor may review it again |
| `accept_manually` | approve | merge first, then complete; the verdict actor is `owner` (`accepted_by=owner`) |
| `return_to_developer` | reject | reopen with the gate note as `review_feedback`; `reject_count` unchanged |
| `cancel_todo` | cancel | supersede the todo |

```bash
loopx gate resolve --goal-id G --todo-id GATE --option accept_manually [--note ...]
loopx todo complete --goal-id G --todo-id GATE --role user --decision-outcome reject \
  --agent-id <acceptor> --note "install the toolchain first"
```

The dashboard `gate.resolve` action takes the same `option` parameter (S7). A
bare decision maps to its default option: approve is retry, reject is return
to developer, cancel is cancel the todo. An option must match its decision.
`accept_manually` is refused while the todo is no longer `in_review`, and the
gate stays open. The applied option is recorded as `decision_option` in the
gate index and as a `review_gate_decided` event.

The owner is not a registered agent, so each option's todo write is
attributed to the todo's claim owner. An unclaimed delivery falls back to the
blocked acceptor, then to the orchestrator. For example, a plan todo without
a `bound_agent` is unclaimed, because Turns do not claim todos. Before this
rule, `accept_manually` on such a todo merged the delivery and closed the
gate, then failed to complete the todo with `agent_id='owner' is not
registered`. The rule is `loopx.agent_registry.lifecycle_agent_for_owner_write`,
which owner acceptance-criteria edits and `todo supersede --by` share.

## Per-todo acceptance criteria (gap G2)

See [design-v0](design-v0.md), decisions 9, 12 and 30.

Plan cards used to keep a todo's `acceptance` in the note, and a Turn
completion overwrites the note with the developer's `next_action`, so the
acceptor never saw the criteria (E2E pilot). The criteria now live in their own
todo field.

| field | type | notes |
|---|---|---|
| `acceptance_criteria` | text | optional; one line, at most 1000 characters. Old todos without it behave as before |

The field is part of `coordination_state_contract_v0.json` (`todo_read_record`)
as its own contract revision, so canonical heads written before it stay
readable. It rides the role-contract patch, so the Markdown codec, the
TypeScript field planner and the canonical providers share one path. The
state event log carries it on `todo_added` and `todo_updated`, Markdown
backfill emits it, and replay folds it.

**Writers.**

- Applying a plan card writes each item's `acceptance` into the field.
- `loopx todo add --acceptance-criteria TEXT`.
- An approved plan card with `criteria_changes` (decision 40, see
  [below](#criteria-changes-need-a-plan-card-decision-40)).
- `loopx todo update --goal-id G --todo-id T --acceptance-criteria TEXT` (or
  `--clear-acceptance-criteria`) by the owner, with no agent id, on its own,
  without other fields. The Python API is
  `loopx.todo_acceptance_criteria.set_goal_todo_acceptance_criteria`.
- Under role_v1 only the goal orchestrator, or the owner with no agent id,
  may write the field on a new todo (`todo add`). A developer or acceptor is
  refused with `acceptance_criteria_requires_orchestrator`, through every
  surface: the CLI, `add_goal_todo` and `update_goal_todo`.
- On an existing todo, under role_v1 with a registered orchestrator, every
  agent, the orchestrator included, is refused with
  `acceptance_criteria_change_requires_plan`; the message points the
  orchestrator to `loopx plan propose` with `criteria_changes`. A role_v1 goal
  without an orchestrator keeps the rule above, and `peer_v1` goals keep the
  previous behaviour (any agent may write).
- An owner or plan-card write on a todo that a developer has claimed is
  attributed, as a lifecycle write, to the claim owner (else the goal
  orchestrator), as a gate decision is, so the kernel's claim fence and task
  leases are unchanged. The real author (`null` for the owner) and the
  `source` (`owner`, `plan_card` or `agent`) are recorded in the returned
  `acceptance_criteria_change` packet.
- Delivery, verdicts, escalation and the Turn writeback never write the
  field: their role-contract patches name only their own fields.

**Change audit (decision 12).** Changing acceptance criteria is a major
change. A CLI update appends a `todo_update` rollout event whose details carry
`acceptance_criteria_changed`, `acceptance_criteria_change_class=major`, the
author, the source, and the previous and new SHA-256 digests. A plan-card
change appends a `todo_criteria_change` event instead. Neither event carries
the criteria text. The gap that the edit was recorded but not gated is closed
by decision 40.

**Readers.** The Turn decision reads the selected todo's durable
`acceptance_criteria` (and the enabled goal acceptance contract, on canonical
goals) before the envelope is signed, only on role_v1 goals. The developer
prompt asks it to meet every criterion. The acceptor prompt asks it to check
each criterion and the goal contract, and, when it rejects, to name each
criterion that failed. The role board card shows the criteria.

## Dependency release and supersession (gap G6)

See [design-v0](design-v0.md), decision 37, and
[gates-plans-intake](gates-plans-intake-v0.md#dependency-release-and-supersession-gap-g6).

In the E2E pilot the orchestrator replaced a twice-rejected frontend todo and
closed the old one as `done`. That released the dependent integration todo
before the replacement was accepted and merged, and the integration branch was
cut from a stale task branch.

**Release rule (role_v1).** A deferred plan todo is reopened only when every
one of its dependencies is satisfied:

- A dependency that requires acceptance (an explicit `requires_acceptance`, or
  the developer default when the goal has an acceptor, the same test that
  sends a delivery to `in_review`) must be `done` with the accept verdict's
  record: completion evidence that starts with `accepted_by=<acceptor>`, where
  the acceptor is the todo's resolved acceptor or one of the goal's acceptors.
  Accept merges the todo branch before it completes the todo, so this is also
  the merge record.
- The owner's manual accept through an acceptor-blocked gate (G12, option
  `accept_manually`) records `accepted_by=owner` after the same merge-first
  path, so it counts too. The gate's `cancel_todo` option supersedes the todo
  without a replacement, which keeps its dependents waiting until the
  orchestrator runs `todo supersede --by`.
- `done` through any other path (a developer or owner completing a blocked
  todo, for example) does not count. The dependent keeps waiting.
- A superseded todo never counts as done, even when it has no replacement.
- A dependency without acceptance is satisfied by `done`, as before.

`peer_v1` goals keep the previous rule: `done` releases dependents.

**Why a todo waits.** `loopx todo list` adds `dependency_waits` (JSON) and a
`## Dependency waits` section (Markdown), and each waiting row carries
`dependency_wait`. The role board card of a deferred todo shows the reason.
The reasons are:

- `waiting for dependency T (status S)`;
- `dependency T is done without an accept+merge record ...`;
- `dependency T was superseded without a replacement ...`.

**Supersede.**

```bash
loopx todo supersede --goal-id G --todo-id OLD --by NEW [--agent-id ORCH] [--note why]
loopx todo supersede --goal-id G --todo-id OLD --by NEW1,NEW2 --agent-id ORCH   # split
```

The Python API is `loopx.plan_dependencies.supersede_goal_todo_by`.

- The replacements must be existing, unfinished agent todos; create them first
  with `todo add`. `--by` takes comma-separated or repeated ids (at most 8).
- OLD is closed through the kernel's own supersede transition: status `done`,
  completion note `superseded`, reason `--note` (else `superseded by NEW`). No
  status is added, and the transition is not counted as done for release.
  An OLD that is already `done` (for example closed by hand) is not closed
  again, but its dependents are still rewired.
- Every todo that depended on OLD now depends on all of the replacements. A
  deferred dependent whose `resume_when` named OLD is updated to name the last
  replacement.
- Only the goal orchestrator, or the owner with no agent id, may supersede.
  Others are refused with `not_orchestrator`. On a `peer_v1` goal `--by` is
  refused (`role_v1_required`); plain `todo supersede` is unchanged.
- Plain `todo supersede --agent-id ORCH` (without `--by`) by the role_v1 goal
  orchestrator no longer needs the claim owner's `--agent-id` (pilot v1 gap
  N11): its kernel write is attributed to the todo's claim owner, like the
  `--by` path, and the `todo_supersede` event keeps the orchestrator as the
  actor. Any other agent still meets the kernel's claim rule, and `peer_v1`
  goals are unchanged.
- A replacement that is itself a dependent of OLD is refused
  (`dependency_cycle`), as is a finished or superseded replacement.
- Retrying the same supersede is a no-op (`already_superseded`); a different
  `--by` for the same OLD is refused.
- `--by` takes only `--note`, `--agent-id` and `--dry-run`. The `--next-*`
  successor options belong to plain `todo supersede`.

As with verdicts, the kernel writes are attributed to the claim owner of OLD
and of each rewired dependent, so claim fences and leases are unchanged. An
unclaimed OLD falls back to the orchestrator, because the owner is not an
agent. An unclaimed dependent falls back to the agent that proposed its plan,
then to the orchestrator. The orchestrator's authority is checked first and
recorded as `actor`, which is `null` for the owner.

**Durable state and replay.** Each supersession is one line of
`<runtime_root>/goals/<goal>/plans/supersessions.jsonl` (schema
`loopx_todo_supersession_v0`: `superseded_todo_id`, `by_todo_ids`,
`rewired_todo_ids`, `actor`, `note`, `at`). Readers fold the log into a
substitution map (first record per todo wins; a torn final line is skipped),
and a todo's effective dependencies are its plan card's `depends_on` with the
map applied transitively. The plan records are never rewritten. Each
supersession also appends one `todo_dependency_rewrite` rollout event, and the
CLI appends its usual `todo_supersede` event. Each resume pass refreshes
`dependency-waits.json` next to the log, a read model for the role board.

The dispatcher's orchestrator prompt says to use `todo supersede` when
replacing or splitting a todo, and never to mark the replaced todo done.

## Criteria changes need a plan card (decision 40)

See [design-v0](design-v0.md), decisions 12 and 40, and
[gates-plans-intake](gates-plans-intake-v0.md#acceptance-criteria-changes-decision-40).

After initial planning, a change to a todo's acceptance criteria takes effect
only through a user-approved plan card. The criteria are the contract the
acceptor holds the developer to, so the user, not an agent, changes them.

- **Initial criteria are unaffected.** Criteria written when a plan card is
  applied (the item's `acceptance`), and criteria on a new todo (`todo add`),
  are written directly; they were approved with the plan.
- **Direct edits.** On an existing todo, `todo update --acceptance-criteria`
  and `update_goal_todo` refuse every agent under role_v1 with
  `acceptance_criteria_change_requires_plan`. The owner (no agent id) may
  still edit directly; the edit is logged as a major change.
- **The plan-card path.** The orchestrator runs `loopx plan propose` with a
  plan file whose `criteria_changes` list has `{todo_id, new, reason}` entries
  (`old` optional). The card may carry only criteria changes or batch them
  with new todos. Approve applies them; reject or cancel changes nothing and
  opens one orchestrator action todo, so the dispatcher wakes the
  orchestrator. An entry whose criteria changed after the proposal is refused
  as `stale` (compared with `old`), and the orchestrator is told the same way.
- **The acceptor waits.** While a card with a change for a todo is `pending`
  (or `applying`), the acceptor does not review that todo:
  - should-run gives a role_v1 acceptor `criteria_change_pending_todo_ids`
    in its identity;
  - role-aware selection holds the in_review todo from the acceptor and
    reports it under `role_scope.review_held` with reason
    `criteria_change_pending`;
  - a pinned `turn run-once --todo-id` does not select it;
  - the dispatcher skips it with reason `criteria_change_pending`, and when
    the acceptor has nothing else, its `no_selected_todo` or
    `should_run_false` skip lists `criteria_change_pending_todo_ids`.

  The developer keeps working on the todo, and other todos continue. Once the
  card is approved, rejected or cancelled, the acceptor reviews it against the
  criteria then in force.
- **Visibility.** The role board card carries `criteria_change_plan_id` and
  `criteria_change_gate_todo_id`, and the dashboard links the card to the plan
  gate ("review on hold"). `plan show`, `gate show` and the dashboard gate
  panel show the old and new criteria side by side.
- **peer_v1** goals are unaffected.
