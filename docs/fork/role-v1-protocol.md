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
`--task-repo NAME` (repeatable) and `--clear-task-repos`.

The Python API takes one `role_contract` patch. In that patch, present keys
are written and `None` clears a field. The same patch reaches the TypeScript
field planner (`role_contract` intent), so the Markdown and canonical
authority paths use a single codec.

The `in_review` status is not part of S1. It is added in slice S2.

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
