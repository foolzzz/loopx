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
empty or blank summary it is the blocked verdict instead. A
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
  the todo stays open. The default `soft_claim` mode works.
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
- `loopx todo update --goal-id G --todo-id T --agent-id ORCH
  --acceptance-criteria TEXT` (or `--clear-acceptance-criteria`), on its own,
  without other fields. The Python API is
  `loopx.todo_acceptance_criteria.set_goal_todo_acceptance_criteria`.
- Under role_v1 only the goal orchestrator, or the owner with no agent id,
  may write the field. A developer or acceptor is refused with
  `acceptance_criteria_requires_orchestrator`, through every surface: the
  CLI, `add_goal_todo` and `update_goal_todo`. Agents without a registered
  role and `peer_v1` goals keep the previous behaviour.
- The orchestrator edits todos that a developer has claimed. As with the
  acceptor's verdicts, the lifecycle write is attributed to the claim owner,
  so the kernel's claim fence and task leases are unchanged. The
  orchestrator's authority is checked first and recorded as `author` in the
  returned `acceptance_criteria_change` packet.
- Delivery, verdicts, escalation and the Turn writeback never write the
  field: their role-contract patches name only their own fields.

**Change audit (decision 12).** Changing acceptance criteria is a major
change. A CLI update appends a `todo_update` rollout event whose details carry
`acceptance_criteria_changed`, `acceptance_criteria_change_class=major`, the
author, and the previous and new SHA-256 digests. The event never carries the
criteria text. Known gap: the edit is recorded, not gated. A plan card is the
preferred path for a criteria change, and the orchestrator prompt says so, but
`todo update --acceptance-criteria` does not require an approved plan.

**Readers.** The Turn decision reads the selected todo's durable
`acceptance_criteria` (and the enabled goal acceptance contract, on canonical
goals) before the envelope is signed, only on role_v1 goals. The developer
prompt asks it to meet every criterion. The acceptor prompt asks it to check
each criterion and the goal contract, and, when it rejects, to name each
criterion that failed. The role board card shows the criteria.
