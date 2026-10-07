"""Actual Chat entrypoint against a fenced FileAuthorityStore and projection IO."""
import hashlib
import json

import pytest

from canonical_authority_fixture import initialize_canonical_authority
from loopx.chat_actions import ChatActionService
from loopx.chat_action_store import ChatActionStore
from loopx.control_plane.coordination.coordination_state_contract import (
    TODO_DOMAIN_READ_RECORD_SCHEMA_VERSION, TODO_DOMAIN_RECORD_FIELDS,
)
from loopx.control_plane.coordination.local_authority import read_canonical_todos_if_promoted
from loopx.control_plane.coordination.authority_projection import canonical_bytes
from loopx.control_plane.todos import provider_projection


@pytest.fixture
def canonical_team(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state.md"
    state.write_text('# Goal\n\n## Agent Todo\n\n')
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"common_runtime_root": str(runtime), "goals": [{
        "id": "goal-a", "repo": str(tmp_path), "state_file": "state.md",
        "coordination": {"registered_agents": ["alpha", "beta"], "agent_model": "peer_v1"}}]}))
    initialize_canonical_authority(runtime, "goal-a", {"goal_id": "goal-a", "todos": [], "leases": [],
        "todo_read_model": {"schema_version": TODO_DOMAIN_READ_RECORD_SCHEMA_VERSION,
            "contract_fields": list(TODO_DOMAIN_RECORD_FIELDS), "todo_count": 0,
            "records_sha256": hashlib.sha256(canonical_bytes([])).hexdigest()}}, state_path=state)
    service = ChatActionService(store=ChatActionStore(runtime / "chat/actions"), registry_path=registry)
    plan = {"schema_version": "steward_team_plan_preview_v0", "kind": "steward_team_plan_preview",
        "goal_id": "goal-a", "objective": "Independent lane outcomes", "quota_envelope": {"slots": 2},
        "stop_condition": "Owner ends request", "lanes": [{"lane_id": f"lane-{agent}", "agent_id": agent,
            "acceptance": "Return independent evidence", "first_todo": {"text": "Same work", "priority": "P1",
            "task_class": "advancement_task", "action_kind": "implement"}} for agent in ("alpha", "beta")]}
    preview = service.preview({"action_kind": "team.plan", "summary": "Assign work", "context": {},
        "normalized_parameters": {"goal_id": "goal-a", "plan": plan}, "idempotency_key": "team-confirm"})
    return runtime, state, service, preview


@pytest.mark.parametrize("failure_point", ["write", "confirmation"])
def test_canonical_chat_commit_replays_after_projection_failure(canonical_team, monkeypatch, failure_point):
    runtime, state, service, preview = canonical_team
    def fail(*args, **kwargs):
        raise OSError("display unavailable")
    if failure_point == "write":
        monkeypatch.setattr(provider_projection, "atomic_write_state_text", fail)
    else:
        read_authority = provider_projection.read_canonical_todos_if_promoted
        def read_after_write(**kwargs):
            if kwargs.get("projection_readback") is not None:
                raise OSError("confirmation unavailable")
            return read_authority(**kwargs)
        monkeypatch.setattr(provider_projection, "read_canonical_todos_if_promoted", read_after_write)
    failed = service.apply(preview["proposal_id"])["proposal"]
    assert failed["status"] == "failed"
    read = read_canonical_todos_if_promoted(runtime_root=runtime, goal_id="goal-a")
    assert len(read["todos"]) == 2
    assert len({row["todo_id"] for row in read["todos"]}) == 2
    assert ("Same work" not in state.read_text()) is (failure_point == "write")
    monkeypatch.undo()
    applied = service.apply(preview["proposal_id"])["proposal"]
    assert applied["status"] == "applied", applied
    assert state.read_text().count("Same work") == 2
    assert read_canonical_todos_if_promoted(runtime_root=runtime, goal_id="goal-a")["provider_revision"] == read["provider_revision"]


def test_canonical_change_without_markdown_refresh_invalidates_preview(canonical_team):
    runtime, state, service, preview = canonical_team
    from loopx.todos import add_goal_todo
    before = state.read_bytes()
    # The public owner commits and projects new work; restoring only the display
    # simulates delayed projection, not a rollback of canonical authority.
    add_goal_todo(registry_path=service.registry_path, goal_id="goal-a", role="agent", text="Concurrent work",
                  task_class="advancement_task", action_kind="implement", agent_id="alpha")
    state.write_bytes(before)
    result = service.apply(preview["proposal_id"])["proposal"]
    assert result["status"] == "stale", result
    assert len(read_canonical_todos_if_promoted(runtime_root=runtime, goal_id="goal-a")["todos"]) == 1
