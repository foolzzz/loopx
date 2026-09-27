# Gate threads, plan cards and goal intake (S6)

Status: implemented in slice S6. This slice covers decisions 10, 11, 12, 13 and 21 of
[design-v0](design-v0.md); acceptance-criteria changes (decision 40) were added later. It builds on the role_v1 roles (S1, see
[role-v1-protocol](role-v1-protocol.md)), the goal `repos` list (S5, see
[workspaces-v0](workspaces-v0.md)) and the web gate apply path (S7).

## Gate discussion threads (decision 10)

Every user gate (`role=user`, `task_class=user_gate`) has an append-only
discussion thread. Each message has the form `{seq, message_id, author: user|orchestrator,
agent_id, text, at}`.

```sh
loopx gate reply --goal-id G --todo-id T --text "What are the tradeoffs?"            # owner
loopx gate reply --goal-id G --todo-id T --as orchestrator --agent-id ORCH --text "..."
loopx gate show  --goal-id G --todo-id T            # status, kind, awaiting, thread
loopx gate list  --goal-id G [--awaiting user|orchestrator]
```

- A user reply is an owner action, so it takes no `--agent-id`. An orchestrator
  reply must come from the goal's role_v1 orchestrator. Every other agent is refused.
- The thread only accepts replies while the gate is `open` or `blocked`. Once
  the gate closes, the thread is read-only.
- Replying never closes a gate. You still close it with approve, reject or cancel,
  either through `loopx gate resolve --goal-id G --todo-id T --decision
  approve|reject|cancel` (the lifecycle actor defaults to the agent the gate
  blocks), `loopx todo complete --role user --decision-outcome ... --agent-id
  <the agent the gate blocks>` (a multi-agent goal needs the lifecycle actor; the
  dashboard path uses the same attribution) or through the dashboard
  `gate.resolve` action (S7). An `acceptor_blocked` gate (G12) also takes
  `--option` / `option`; see
  [role-v1-protocol](role-v1-protocol.md#acceptor-verdicts-and-isolation-gap-g12).
- A gate whose thread awaits the orchestrator (the user replied last) does not
  block the orchestrator's lane under role_v1, so the reply can be answered. Once
  the orchestrator replies, the gate awaits the user and blocks it again. The
  orchestrator's quota identity carries `awaiting_orchestrator_gate_ids` for this.

### Awaiting state and the dispatcher contract (S4)

`awaiting` is derived from the last message:

| last message | awaiting |
|---|---|
| none | `awaiting_user` |
| user | `awaiting_orchestrator` |
| orchestrator | `awaiting_user` |
| gate closed | `closed` |

The thread state is durable under the runtime root:

- `goals/<G>/gates/<todo_id>.jsonl` holds the thread. Lines are only ever appended,
  under a lock.
- `goals/<G>/gates/index.json` holds one entry per gate:
  `{kind, plan_id?, awaiting, message_count, last_author, last_at, closed,
  decision_outcome?}`. It is updated in the same lock as the append. The
  dispatcher can watch this file, or call
  `loopx.gate_threads.gates_awaiting_orchestrator(runtime_root, goal_id)`.
- Every reply also appends a `gate_thread_reply` event to the goal's
  `rollout-event-log.jsonl`. The event carries the gate id, the author in
  `lane.agent_role`, the new awaiting state in `state_transition.to_state`, and the
  seq and message id. It never carries the message text.

When a gate closes through any path that uses `complete_goal_todo` (the CLI, the
dashboard or any library caller), its index entry is marked `closed`.

The S4 dispatcher fingerprints the files directly under `goals/<G>/`, including
the rollout log and the `gates/` directory, so a user reply wakes the
orchestrator. The orchestrator's system-prompt addendum explains the gate and
plan commands and names the gates that are `awaiting_orchestrator`.

### Web

The dashboard context drawer shows a **Discussion** panel for every user gate.
The panel lists the thread, shows who it is waiting for, and has a reply box.
It is backed by two chat server endpoints, which accept loopback requests only:

- `GET /api/chat/gate-thread?goal_id=G&todo_id=T` returns what `gate show` returns.
- `POST /api/chat/gate-thread/reply` with body `{goal_id, todo_id, text}` adds an
  owner reply. It returns 409 once the gate is closed, and 400 if the body has
  unknown fields such as `author`.

## Only the orchestrator opens user gates (decision 11)

Under role_v1, a developer or acceptor agent cannot open a user todo. This covers
`todo add --role user --agent-id AGENT`, and `--next-user-todo` on `todo complete`
and `todo supersede`, on both Markdown and canonical-authority goals. The
error tells the agent to raise a blocker instead:

```sh
loopx todo add --goal-id G --role agent --task-class blocker --text "Which DB should the API use?"
```

Blockers route to the orchestrator (S1 effective role). The orchestrator answers
from the requirement docs, or opens a gate itself. The following can still open
gates:

- the owner or CLI when no agent id is given;
- LoopX itself: the dispatcher's re-login and cooldown gates (decision 17), and
  the `acceptor_blocked` gate a blocked acceptor verdict opens (G12);
- the orchestrator;
- agents that have no registered role;
- every agent on `peer_v1` goals.

## Plan cards (decision 12)

```sh
loopx plan propose --goal-id G --agent-id ORCH --plan-file plan.json [--revise PLAN_ID]
loopx plan show    --goal-id G --plan-id PLAN_ID
loopx plan list    --goal-id G [--require-status applied]   # exit 1 when no plan has it
loopx plan apply   --goal-id G --plan-id PLAN_ID      # recovery only
```

Plan file format:

```json
{
  "title": "Todo app v1",
  "summary": "Contract first, then API and web, then integration.",
  "todos": [
    {"key": "contract", "text": "Write the API contract", "required_role": "developer",
     "bound_agent": "dev", "acceptor_agent": "acc", "task_repositories": ["api"],
     "acceptance": "openapi covers CRUD", "validation_command": "test -f openapi.yaml",
     "estimated_effort": "1h", "priority": "P1", "requires_acceptance": true},
    {"key": "api", "text": "Implement the API", "depends_on": ["contract"], "task_repositories": ["api"]},
    {"key": "web", "text": "Implement the web UI", "depends_on": ["contract"], "task_repositories": ["web"]},
    {"key": "integrate", "text": "Integrate web and API", "depends_on": ["api", "web"],
     "task_repositories": ["api", "web"]}
  ]
}
```

Each todo accepts these fields:

- `key`, `text`
- `priority` (`P0` to `P3`)
- `required_role` (developer, acceptor or orchestrator; the default is developer)
- `bound_agent`, which must be registered and have that role
- `acceptor_agent`, which must be an acceptor
- `depends_on`, and its inverse `successors`
- `requires_acceptance`
- `task_repositories`, which must name repos the goal declares
- `acceptance` (at most 1000 characters; it becomes the todo's `acceptance_criteria`),
  `validation_command`, `estimated_effort`, `action_kind`

Unknown fields are refused. Todos must be listed in dependency order.

A plan may also carry `criteria_changes` for existing todos (decision 40, see
[below](#acceptance-criteria-changes-decision-40)). A plan with criteria
changes may have no `todos`.

Lifecycle:

1. **propose.** The goal's orchestrator is the only agent that can propose. The
   plan is validated, including a dry run of every todo add. It is stored at
   `<runtime_root>/goals/<G>/plans/<plan_id>.json` with `status=pending`. A
   `plan_approval` user gate is opened and blocks the orchestrator's lane, and the
   orchestrator posts the first thread message. Proposing an identical plan again
   returns the existing pending card.
2. **discuss.** The user requests changes in the gate thread. The orchestrator
   revises the plan in place with `--revise PLAN_ID`. This increments `revision`,
   keeps the earlier revisions and the same gate, and posts a thread message.
3. **approve.** Before the gate is allowed to close, the plan is validated again.
   If it no longer validates, for example because a repo or agent was removed,
   the approve is refused and the gate stays open. After the gate closes, the plan
   is applied. Todos are created in plan order, and `todo_id_map` maps each plan key
   to its todo id. Plan fields map to todo fields as follows:
   - `bound_agent` becomes `claimed_by`.
   - The role fields map to the S1 role contract.
   - `acceptance` goes into the todo's orchestrator-owned `acceptance_criteria`
     field (gap G2), which a Turn completion never overwrites. Developer and
     acceptor Turns see it on every Turn. See
     [role-v1-protocol](role-v1-protocol.md#per-todo-acceptance-criteria-gap-g2).
   - `estimated_effort` and the full dependency list go into the note.
   - A todo with dependencies is created `deferred` with
     `resume_when=todo_done:<id of its last listed dependency>`. `resume_when`
     holds only one condition, and in dependency order the last listed
     dependency is the latest one.
   - `resume_ready_plan_todos` reopens a deferred plan todo once **all** of its
     `depends_on` todos are satisfied (see
     [below](#dependency-release-and-supersession-gap-g6)). The accept verdict
     and every dispatcher pass call it.
4. **reject / cancel.** Nothing is applied, and the plan becomes `rejected` or
   `cancelled`.

A plan is applied exactly once, and how depends on the goal's authority:

- **Markdown goals.** The whole batch is written in one locked state-file write,
  so either every todo is created or none is.
- **Promoted canonical goals.** The file and sqlite providers create the todos in
  order, each with the deterministic operation id `plan-<plan_id>-<key>`. If the
  apply is interrupted, the plan stays `applying`, and the next attempt (settlement
  or `loopx plan apply`) replays the same operation ids without creating
  duplicates.

Changing a todo's acceptance criteria after the plan is applied is a major
change (decision 12) and goes through a plan card (decision 40, below).

An applied plan is never re-applied. This slice does not add a single-CAS canonical
batch. That would be an extension of `work_items/team_plan.ts`, whose lane model
has one advancement todo per lane, no role, dependency or validation fields, and
an `actor === lane.agent_id` rule. Plan application here is an owner-confirmed
action with no actor, which resolves the orchestrator-assigns-others concern
raised in S1 without granting the orchestrator anything new.

## Dependency release and supersession (gap G6)

Decision 37. On role_v1 goals a dependency that requires acceptance is
satisfied only when it is accepted and merged: `done` with the accept
verdict's `accepted_by=<acceptor>` evidence (accept merges before it
completes). A manual `done` keeps its dependents waiting, a superseded todo
never counts as done, and a dependency without acceptance is satisfied by
`done`. `peer_v1` keeps the old rule.

To replace or split a todo, the orchestrator creates the new todo(s) and runs
`loopx todo supersede --goal-id G --todo-id OLD --by NEW[,NEW2] --agent-id
ORCH`. OLD is closed as superseded, and every todo that depended on OLD now
depends on all of the new ones. The rewrite is an append-only supersession log
next to the plan cards, applied on top of each applied plan's `depends_on`.

`loopx todo list` and the role board say why a deferred plan todo is still
waiting. Details: [role-v1-protocol](role-v1-protocol.md#dependency-release-and-supersession-gap-g6).

## Acceptance-criteria changes (decision 40)

After initial planning, a change to a todo's `acceptance_criteria` takes
effect only through a user-approved plan card. Under role_v1, `loopx todo
update --acceptance-criteria` refuses every agent, the orchestrator included,
with `acceptance_criteria_change_requires_plan`; only the owner (no agent id)
still edits directly. Criteria written at plan apply and on new todos are
unaffected. See
[role-v1-protocol](role-v1-protocol.md#criteria-changes-need-a-plan-card-decision-40)
for selection and the dispatcher.

The orchestrator proposes the change through the ordinary plan-card flow:

```json
{
  "title": "Page the orders list",
  "criteria_changes": [
    {"todo_id": "todo_…", "new": "GET /orders returns 200 paged by 50",
     "reason": "the user asked for paging"}
  ],
  "todos": []
}
```

Each entry has `todo_id`, `new` (null clears the criteria), `reason` and an
optional `old`. At most 20 entries; one per todo. The card can batch criteria
changes with new `todos`.

1. **propose.** Each entry's `old` is set to the todo's current criteria. A
   supplied `old` that no longer matches is refused as `criteria_change_stale`.
   An unknown or closed todo, a user todo, a change that sets the current value,
   and a todo that already has a change on another pending card
   (`criteria_change_already_pending`; revise that card instead) are refused.
   The gate text counts the changes, for example `(0 todos, 1
   acceptance-criteria change)`.
2. **discuss.** `loopx plan show` and `loopx gate show` print an
   "Acceptance-criteria changes" table with the old and new criteria side by
   side. The dashboard gate panel shows the same table (`criteria_changes` in
   `GET /api/chat/gate-thread`). While the card is pending, the acceptor does
   not review the todo; the developer keeps working.
3. **approve** (CLI, `loopx gate resolve` or the dashboard `gate.resolve`).
   New todos are created first, then each entry is applied once and recorded
   in the plan record's `criteria_change_results` (`applied`, `stale` or
   `refused`). Each entry appends a `todo_criteria_change` rollout event with
   the plan id, revision, `approved_by=user` and the old and new digests
   (never the text). An entry whose todo's criteria changed after the
   proposal (compared with `old`) is refused as `stale`; the other entries
   and the new todos still apply. The field write is attributed like an owner
   edit: the lifecycle actor is the claim owner, else the orchestrator.
4. **reject / cancel.** Nothing changes.

After a reject, a cancel, or a stale or refused entry, LoopX opens one
orchestrator action todo (`Orchestrator action: the acceptance-criteria
change of plan … was rejected …`, claimed by the orchestrator). The dispatcher
launches the orchestrator for it through the ordinary action-todo path, and
its validator passes once the orchestrator has changed some other todo, for
example a revised plan card, updated review feedback, or a user gate. The
user's decision note is kept in the todo's note.

## Goal intake (decisions 13 and 21)

```sh
loopx goal create --project <state-home> --goal-id G --doc requirements.md \
  --repo api=/abs/api --repo web=../web,default_branch=main \
  --agent orch=orchestrator --agent dev=developer --agent acc=acceptor \
  [--objective TEXT] [--no-global-sync] [--dry-run]
```

The state home can be a project directory or a central progress repo that is
separate from the code repos. The command does the following:

1. Bootstraps the goal in the state home if it does not exist yet. The registry is
   `<state-home>/.loopx/registry.json` unless `--registry` is given. The objective
   defaults to the doc's first heading.
2. Copies the doc to `<state-home>/docs/goals/<G>/<name>` and makes it the goal
   doc. It also registers the doc as authority source `goal-requirements` (role
   `requirements`, private and redacted).
3. Registers the agents and their roles with `agent_model=role_v1`. Exactly one
   orchestrator is required. It also declares the `repos`; relative paths resolve
   against the current directory.
4. Adds the orchestrator's first todo. The todo has `action_kind=plan`,
   `required_role=orchestrator`, is claimed by the orchestrator and has
   `requires_acceptance=false`. It asks the orchestrator to clarify the
   requirements through gate threads and then propose a plan card. The command
   also appends a `goal_intake` rollout event. The todo and the event are the
   durable trigger for the orchestrator's first turn.

Running the command again with the same arguments is safe: each step is an upsert.

The dashboard intake form described in decision 13 is not part of this slice.
