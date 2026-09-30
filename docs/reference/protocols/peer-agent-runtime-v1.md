# Peer Agent Runtime v1

## Purpose

`peer_v1` removes durable agent rank from LoopX runtime decisions. Registered
agents have equal identity authority. Work ownership comes from todo claims,
task leases, explicit continuation policy, and bounded task-scoped assignment.

Functional profiles may still describe capabilities or preferred scopes. They
must not grant one identity implicit review, merge, routing, or replan authority
over every other identity.

## Canonical Identity

A peer identity contains:

```json
{
  "schema_version": "peer_agent_identity_v1",
  "agent_model": "peer_v1",
  "agent_id": "codex-alpha",
  "registered": true,
  "registered_agents": ["codex-alpha", "codex-beta"]
}
```

Canonical peer output must not contain `primary_agent`, `handoff_agent`, or a
rank-bearing `role` field, including null-valued placeholders.

## Work Ownership

1. An explicit todo `claimed_by` or active task lease wins.
2. An unclaimed todo must be claimed or leased before delivery.
3. An explicitly agent-scoped replan obligation stays with that agent.
4. An unscoped replan obligation is assigned to exactly one registered peer by
   hashing a canonical work key over the sorted registered-agent set.
5. Registration order must not change deterministic assignment.

The deterministic assignment is coordination for one work item. It does not
change identity authority and must not be persisted as an agent rank.

## Completion And Review

Continuation behavior is task policy:

- `independent_handoff`: leave the successor unclaimed unless an explicit peer
  is selected;
- `same_agent_non_delivery`: keep the successor with the completing peer.

Review is an `action_kind`, not a continuation type. Use an ordinary
`independent_handoff`; when the task must stay open for any eligible peer except
the author, add that author to `excluded_agents`. An explicit `claimed_by` must
never name an excluded peer.

Repository merge permission remains governed by the repository's maintainer
policy. Peer identity alone neither grants nor removes self-merge permission.
The canonical completion flag is `--self-merged`.

## Workspace Isolation

Every peer is subject to the same workspace rule. `agent_workspace_guard_v1`
requires an independent git worktree when the selected todo declares write
scopes, uses a write-class action kind, or goal policy explicitly requires
isolation. Read-only observation and monitor work do not trigger the guard by
identity alone.

For a task owned by a repository other than the goal repository, the agent todo
may declare `task_repository` as a credential-free canonical identity such as
`git:github.com/owner/repo`. The guard then requires a linked worktree whose
origin matches that identity. The field selects the repository used for
workspace isolation only: it is not an agent scope, write scope, permission
grant, or replacement for claim/lease and goal-boundary checks. Without the
field, the goal repository remains authoritative.

## Task-Scoped Coordination

When bounded multi-agent orchestration is enabled, LoopX hashes the canonical
task bundle and selects one temporary coordinator. The resulting
`task_orchestration_contract_v1`:

- is scoped to that task bundle;
- activates or resumes eligible peer lanes;
- gives the coordinator writeback responsibility only for accepted bundle
  evidence;
- does not make the coordinator a durable leader.

## Retired v0.1 hierarchy input

LoopX no longer migrates the v0.1 main/side hierarchy. A goal that still
contains `legacy_hierarchy`, `primary_agent`, `side_agent_handoff_agent`,
`agent_profile_v0`, or profile-level hierarchy policy fails before identity or
work routing. The diagnostic lists every rejected field. Remove those fields
from the source registry, set `coordination.agent_model` to `role_v1` or
`peer_v1`, retain the current `coordination.registered_agents` roster, and
rerun the command. A subsequent `quota should-run` must use the registered
`--agent-id`.

Current goals with a registered roster still fail closed when `quota
should-run` omits `--agent-id`; the identity-aware heartbeat prompt remains the
repair path for that current-state error.

An optional supervisor is an overlay on this peer model, not a replacement for
it. See [Peer Supervisor v0](peer-supervisor-v0.md).
