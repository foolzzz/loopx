"""The dashboard gate.resolve typed action applies through the CLI decision path."""
from __future__ import annotations

import json
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread

import pytest
from canonical_authority_fixture import initialize_canonical_authority

from loopx.chat_action_store import ChatActionStore
from loopx.chat_actions import ChatActionService, ProtectedActionGate
from loopx.control_plane.coordination.runtime_shadow import build_todo_runtime_shadow_projection
from loopx.todos import add_goal_todo, list_goal_todos

PROVIDERS = [None, "file", "sqlite"]


def fixture(tmp_path: Path, provider: str | None) -> tuple[Path, str, str]:
    project = tmp_path / "project"
    project.mkdir()
    state = project / "ACTIVE_GOAL_STATE.md"
    state.write_text("# Goal\n\n## User Todo\n\n## Agent Todo\n\n## Completed Work Archive\n", encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"schema_version": 1, "common_runtime_root": str(tmp_path / "runtime"),
        "goals": [{"id": "goal-a", "status": "active", "repo": str(project), "state_file": state.name,
                   "coordination": {"registered_agents": ["agent-a"]}}]}), encoding="utf-8")
    target = add_goal_todo(registry_path=registry, goal_id="goal-a", role="agent",
        text="Publish after the owner decision", task_class="advancement_task",
        status="blocked", claimed_by="agent-a")
    gate = add_goal_todo(registry_path=registry, goal_id="goal-a", role="user",
        text="Approve publishing", task_class="user_gate", blocks_agent="agent-a",
        unblocks_todo_id=target["todo_id"])
    if provider:
        todos = list_goal_todos(registry_path=registry, goal_id="goal-a")["todos"]
        projection = build_todo_runtime_shadow_projection(goal_id="goal-a", todos=todos, handoff_mode="soft_claim")
        initialize_canonical_authority(tmp_path / "runtime", "goal-a", projection, state_path=state, provider=provider)
    return registry, str(gate["todo_id"]), str(target["todo_id"])


def rows(registry: Path) -> dict[str, dict]:
    return {row["todo_id"]: row for row in list_goal_todos(registry_path=registry, goal_id="goal-a")["todos"]}


def preview(service: ChatActionService, todo_id: str, decision: str, *, note: str | None = "Owner reviewed", key: str = "gate-1") -> dict:
    parameters = {"goal_id": "goal-a", "todo_id": todo_id, "decision": decision}
    if note is not None:
        parameters["note"] = note
    return service.preview({"action_kind": "gate.resolve", "summary": f"{decision} the gate",
        "normalized_parameters": parameters, "context": {}, "idempotency_key": key})


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(("decision", "target_status"), [("approve", "open"), ("reject", "blocked"), ("cancel", "blocked")])
def test_gate_resolve_applies_decision_with_note_and_readback(
    tmp_path: Path, provider: str | None, decision: str, target_status: str,
) -> None:
    registry, gate_id, target_id = fixture(tmp_path, provider)
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=registry)
    proposal = preview(service, gate_id, decision)
    assert proposal["status"] == "preview_ready"
    applied = service.apply(proposal["proposal_id"])["proposal"]
    assert applied["status"] == "applied", applied
    receipt = applied["receipt"]
    assert receipt["outcome"] == "gate_resolved"
    assert (receipt["decision_outcome"], receipt["decided_by"], receipt["surface"]) == (decision, "owner", "local_dashboard")
    assert receipt["gate_readback"] == {"todo_id": gate_id, "status": "done", "decision_outcome": decision,
        "note": "Owner reviewed", "target": {"todo_id": target_id, "status": target_status}}
    state = rows(registry)
    assert (state[gate_id]["status"], state[gate_id]["decision_outcome"], state[gate_id]["note"]) == ("done", decision, "Owner reviewed")
    assert state[target_id]["status"] == target_status


@pytest.mark.parametrize("provider", PROVIDERS)
def test_gate_resolve_reapply_is_idempotent(tmp_path: Path, provider: str | None) -> None:
    registry, gate_id, _target_id = fixture(tmp_path, provider)
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=registry)
    proposal = preview(service, gate_id, "approve")
    first = service.apply(proposal["proposal_id"])["proposal"]
    after_first = rows(registry)
    second = service.apply(proposal["proposal_id"])["proposal"]
    assert second["status"] == "applied"
    assert second["receipt"] == first["receipt"]
    assert rows(registry) == after_first
    # A new proposal against the already-resolved gate is refused at preview.
    with pytest.raises(ValueError, match="already done"):
        preview(service, gate_id, "reject", key="gate-2")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_gate_resolve_recovers_lost_receipt_without_double_write(
    tmp_path: Path, provider: str | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, gate_id, target_id = fixture(tmp_path, provider)
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=registry)
    proposal = preview(service, gate_id, "approve")
    real_apply = service.store.apply
    with monkeypatch.context() as patch:
        def lost_response(*args, **kwargs):
            raise ConnectionError("Synthetic receipt loss after canonical commit")
        patch.setattr(service.store, "apply", lost_response)
        with pytest.raises(ConnectionError):
            service.apply(proposal["proposal_id"])
    assert service.store.apply == real_apply
    committed = rows(registry)
    assert committed[gate_id]["status"] == "done"
    recovered = service.apply(proposal["proposal_id"])["proposal"]
    assert recovered["status"] == "applied"
    assert recovered["receipt"]["gate_readback"]["target"] == {"todo_id": target_id, "status": "open"}
    assert rows(registry) == committed


@pytest.mark.parametrize("provider", [None, "file"])
def test_gate_resolve_defer_is_preview_only(tmp_path: Path, provider: str | None) -> None:
    registry, gate_id, target_id = fixture(tmp_path, provider)
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=registry)
    proposal = preview(service, gate_id, "defer", note=None)
    before = rows(registry)
    with pytest.raises(ProtectedActionGate) as raised:
        service.apply(proposal["proposal_id"])
    assert raised.value.gate["kind"] == "gate_defer_preview_only"
    assert rows(registry) == before
    assert before[gate_id]["status"] == "open" and before[target_id]["status"] == "blocked"


def test_gate_resolve_rejects_unknown_and_non_gate_todos(tmp_path: Path) -> None:
    registry, _gate_id, target_id = fixture(tmp_path, None)
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=registry)
    with pytest.raises(ValueError, match="was not found"):
        preview(service, "todo_missing000", "approve")
    with pytest.raises(ValueError, match="not an owner decision gate"):
        preview(service, target_id, "approve", key="gate-2")
    with pytest.raises(ValueError, match="approve, reject, cancel, or defer"):
        preview(service, target_id, "maybe", key="gate-3")


def test_gate_resolve_stale_preview_does_not_write(tmp_path: Path) -> None:
    registry, gate_id, _target_id = fixture(tmp_path, None)
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=registry)
    reject = preview(service, gate_id, "reject", key="reject")
    approve = preview(service, gate_id, "approve", key="approve")
    assert service.apply(approve["proposal_id"])["proposal"]["status"] == "applied"
    stale = service.apply(reject["proposal_id"])["proposal"]
    assert stale["status"] == "stale"
    assert rows(registry)[gate_id]["decision_outcome"] == "approve"


def test_gate_resolve_over_packaged_http(tmp_path: Path) -> None:
    from loopx.chat_server import ChatHTTPServer, ChatRequestHandler, default_chat_assets_dir

    registry, gate_id, target_id = fixture(tmp_path, "file")
    store = ChatActionStore(tmp_path / "actions")
    server = ChatHTTPServer(("127.0.0.1", 0), ChatRequestHandler)
    server.verbose = False
    server.assets_dir = default_chat_assets_dir()
    server.action_store = store
    server.action_service = ChatActionService(store=store, registry_path=registry)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=45)
    try:
        body = {"action_kind": "gate.resolve", "summary": "Approve publishing", "context": {},
            "idempotency_key": "http-gate", "normalized_parameters": {"goal_id": "goal-a",
                "todo_id": gate_id, "decision": "approve", "note": "Ship it"}}
        connection.request("POST", "/api/actions/preview", body=json.dumps(body), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        previewed = json.loads(response.read())
        assert response.status == 201, previewed
        proposal_id = previewed["proposal"]["proposal_id"]
        connection.request("POST", f"/api/actions/{proposal_id}/apply", body="{}", headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        result = json.loads(response.read())
        assert response.status == 200, result
        assert result["proposal"]["status"] == "applied"
        assert result["proposal"]["receipt"]["gate_readback"]["target"] == {"todo_id": target_id, "status": "open"}
        state = rows(registry)
        assert (state[gate_id]["decision_outcome"], state[gate_id]["note"]) == ("approve", "Ship it")
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
