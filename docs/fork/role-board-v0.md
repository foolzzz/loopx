# Role board (S8)

Status: implemented in slice S8. This slice covers the role board half of the
dashboard item in decision 25 of [design-v0](design-v0.md), using the stage
model from decision 7. It reads the role_v1 roster (S1, see
[role-v1-protocol](role-v1-protocol.md)), the dispatcher state (S4, see
[dispatcher-v0](dispatcher-v0.md)) and gate threads and plan cards (S6, see
[gates-plans-intake-v0](gates-plans-intake-v0.md)).

## Status projection

`loopx status --format json` and the dashboard's `/status.json` attach
`run_history.goals[].role_board` to every goal whose coordination block records
`agent_model=role_v1` or any `agent_roles`. `peer_v1` goals and goals without a
coordination block get no board. The code is in
`loopx/control_plane/status/role_board_projection.py`.

```json
{
  "schema_version": "loopx_role_board_v0",
  "agent_model": "role_v1",
  "dispatcher": {"available": true, "serving": true, "updated_at": 1790000000.0},
  "agents": [{"agent_id": "dev", "role": "developer", "activity": "running",
              "running_todo_ids": ["todo_…"], "running_turns": 1, "provider": "anthropic"}],
  "todos": [{"todo_id": "todo_…", "text": "…", "status": "open", "effective_role": "developer",
             "required_role": "developer", "claimed_by": "dev", "acceptor_agent": "acc",
             "reject_count": 1, "requires_acceptance": true, "task_repositories": ["api"],
             "running": true, "running_agent_id": "dev",
             "plan_id": "plan_…", "plan_gate_todo_id": "todo_…"}],
  "gates": [{"todo_id": "todo_…", "text": "…", "kind": "plan_approval", "awaiting": "awaiting_user",
             "message_count": 1, "plan_id": "plan_…", "plan_status": "pending",
             "plan_title": "…", "plan_revision": 1, "plan_todo_count": 5}],
  "omitted": {"agents": 0, "open_todos": 0, "done_todos": 0, "gates": 0}
}
```

### Agents

The roster is `registered_agents` plus every agent in `agent_roles`. `activity`
is resolved in this order:

| activity | source |
|---|---|
| `running` | a dispatcher run for the agent whose pid is alive, or an unexpired task lease owned by the agent in this goal. If the agent only runs in other goals, `running_goal_ids` names them |
| `unavailable` | an active `agent_cooldowns` entry, such as a failed auth preflight. `reason` holds the preflight status |
| `cooldown` | an active `provider_cooldowns` entry for the provider that the dispatcher recorded in `agent_slots`. `reason` holds the failure kind |
| `idle` | the dispatcher state exists and none of the above applies |
| `unknown` | there is no dispatcher state file under `<runtime-root>/dispatch/` |

### Todos

Todos are the goal's agent todos from the status `todo_index`, so they come
from the same source as the Tasks tab. Continuous monitors and superseded or
cancelled todos are left out. The kernel closes a superseded todo as `done`, so
the board also drops a done todo whose completion note is `superseded`, that
has a `superseded_by` link, or that is named in `plans/supersessions.jsonl`
(`todo supersede --by`); it is replaced work, not finished work.

The `todo_index` status comes from the attention queue, which reads the goal
state file. Rollout events add history (`event_kinds`, `latest_event_*`) but
never overwrite the status of a todo the queue carries, because several
lifecycle writes append no `todo_*` event (`gate resolve`, system gates such as
`acceptor_blocked`, `push_request` and `budget_exhausted`) or one without a
todo status (`todo supersede --by`). A todo known only from events projects
`todo_supersede` as `done` (pilot v1 gap N3). `effective_role` and `requires_acceptance` use
the kernel rules (`todo_effective_required_role`, `todo_requires_acceptance`),
so an absent `requires_acceptance` shows its derived default. `running` is set
when an active dispatcher run or an unexpired lease covers the todo. A todo
that a plan card created carries `plan_id` and the plan's gate id.

The projection passes `status` through unchanged, including `in_review` from
slice S2.

Gap G12 adds two card flags: `review_checkout_modified` when an acceptor Turn
changed its throwaway review checkout (from the dispatcher's
`review_warnings`), and `review_blocked_gate_todo_id` when an open
`acceptor_blocked` gate holds the todo's review. A gate of that kind reports
`kind=acceptor_blocked` with its `review_todo_id` and `options`; the dashboard
falls back to rendering it as a decision.

### Gates

The `gates` list holds open user gates (`role=user`, `task_class=user_gate`).
Their kind, awaiting state and message count come from
`goals/<G>/gates/index.json`. For plan approvals, the plan's title, status,
revision and todo count come from its card. Gates that wait on the user sort
first, and plan approvals sort before decisions.

### Bounds

The projection is bounded as follows:

- at most 16 agents;
- at most 60 open todos, with `in_review`, running and rejected cards kept first
  when the list is cut;
- the 12 most recent done todos;
- at most 10 gates;
- text fields cut to 160 characters.

The `omitted` field reports how many items were cut. Each source degrades on
its own: if one source is missing or unreadable, the board shows `unknown` for
that part and the status read continues. The projection never writes
dispatcher, gate or plan state.

## Dashboard

A goal with a board shows a **Role board** tab ("角色看板") next to **Tasks**.
The code is in `goal-role-board-view.tsx`, with the model in `role-board-model.ts`.

- **Waiting on you.** This list at the top shows open gates and plan approvals.
  Click one to open its drawer, which has the discussion thread and the
  approve, reject and cancel actions from S6 and S7.
- **Columns** are derived stages:

  | column | rule |
  |---|---|
  | Done | `done` or `completed` |
  | In review | `in_review` |
  | Running | an active Turn or lease |
  | Rework | `reject_count > 0` |
  | Assigned | a bound or claimed agent |
  | Planned | none of the above |

  The rules are checked in this order, and the first match wins. Deferred and
  blocked todos follow the same rules and carry a status badge.
- **Swimlanes** are the roles orchestrator, developer and acceptor. Each lane
  shows its agents as chips coloured by activity. A card in review sits in the
  acceptor lane. Every other card sits in the lane of its effective role.
- **Cards** show the title, the running or bound agent, the repos, a
  "Rejected ×N" badge, the acceptor (or "No acceptance"), the todo's
  `acceptance_criteria` (gap G2; at most 300 characters, clamped to two lines,
  full text on hover), for a deferred plan todo why it still waits on its
  dependencies (`dependency_wait`, gap G6; from the resume pass's
  `dependency-waits.json`) and a link to the plan.
  The plan link opens the plan's gate while that gate is open. Clicking a card
  opens the Todo drawer.

The zod schema (`roleBoardSchema` in `src/data/status.ts`) maps unknown enum
values to fallbacks, and turns a malformed board into `null`. As a result, a
newer backend can never blank the status payload.

### Usage strip

Gap G9. When the goal's status row carries `turn_usage_summary` (see
[usage-accounting-v0](usage-accounting-v0.md)), a strip under the board
header shows:
- the goal's cost, with the estimated part named separately;
- agent-hours and Turns;
- cost per accepted todo, with the count (todos with an accept record only),
  and the same figure without the orchestrator's spend when it has any;
- the budget share, amber from 80% and red from 100%;
- the per-role split (cost and Turns).

The zod `turnUsageSummarySchema` turns a malformed summary into `null`, which
hides the strip without breaking the board.

## Verification

```sh
python -m pytest -q tests/control_plane/test_role_board_projection.py
cd apps/presentation/dashboard
npm run smoke:role-board              # schema + stage/swimlane derivation (in smoke:personal-workspace)
npm run smoke:role-board-browser      # renders the tab from smoke/role-board-fixture.json, zh + en
npm run smoke:status-projection-contract
```

## Known gaps

- Canonical-authority goals report leases through the effect runtime, which
  the board does not call. On those goals, `running` reflects dispatcher Turns
  only.
- The todo list inherits the status `todo_index` cap of 240 items across all
  goals.
- The board does not show the acceptor verdict or feedback text. It shows only
  `reject_count`. The S2 accept and reject actions are not wired into the
  board. You act on a card through the Todo drawer.
