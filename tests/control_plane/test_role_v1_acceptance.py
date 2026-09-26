"""role_v1 fork slice S2: the acceptance flow.

Covers delivery -> in_review, the no-acceptance shortcut to done, the
acceptor verdict (accept, reject with feedback, second-reject escalation to
the orchestrator), verdict authorization, role-aware selection of in_review
work, role-aware user-gate blocking, the status projection and event replay.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from loopx.control_plane.agents.identity import build_quota_agent_identity
from loopx.control_plane.todos.contract import todo_review_agent
from loopx.control_plane.todos.quota_summary import (
    select_quota_todo_summary,
    summarize_user_todos_for_quota,
)
from loopx.todo_acceptance import accept_goal_todo, reject_goal_todo
from loopx.todos import add_goal_todo, complete_goal_todo, list_goal_todos


GOAL_ID = "goal-acc"
ORCH = "fable-orch"
DEV = "opus-dev"
ACC = "codex-acceptor"
ROLES = {ORCH: "orchestrator", DEV: "developer", ACC: "acceptor"}


def _setup(tmp_path: Path, *, promoted: bool, acceptors: dict[str, str] | None = None):
    from canonical_authority_fixture import initialize_canonical_authority
    from loopx.control_plane.coordination.runtime_shadow import (
        build_todo_runtime_shadow_projection,
    )
    from loopx.control_plane.testing.canary_harness import write_fixture_registry
    from loopx.control_plane.todos.active_state_todo_parser import parse_active_state_todos

    runtime, registry = tmp_path / "runtime", tmp_path / "registry.json"
    state = tmp_path / "state.md"
    state.write_text(
        "---\nstatus: active\n---\n# Goal\n## Objective\nShip the orders feature.\n\n"
        "## User Todo\n\n"
        "## Agent Todo\n- [ ] [P1] Implement the orders API endpoint.\n"
        "  <!-- loopx:todo todo_id=todo_orders_api role=agent task_class=advancement_task "
        f"status=open claimed_by={DEV} -->\n"
        "- [ ] [P2] Write the research notes for orders.\n"
        "  <!-- loopx:todo todo_id=todo_orders_notes role=agent task_class=advancement_task "
        f"status=open claimed_by={DEV} requires_acceptance=false -->\n",
        encoding="utf-8",
    )
    agents = [ORCH, DEV, ACC, *(acceptors or {})]
    write_fixture_registry(
        project=tmp_path, runtime_root=runtime, registry_path=registry,
        goal_id=GOAL_ID, domain="role-v1", adapter_kind="generic_project_goal_v0",
        state_file=str(state), registered_agents=agents, quota_allowed_slots=None,
    )
    payload = json.loads(registry.read_text())
    coordination = payload["goals"][0].setdefault("coordination", {})
    coordination["agent_model"] = "role_v1"
    coordination["agent_roles"] = {**ROLES, **(acceptors or {})}
    payload["goals"][0]["repos"] = [
        {"name": name, "path": str(tmp_path / name)} for name in ("backend", "frontend")
    ]
    registry.write_text(json.dumps(payload), encoding="utf-8")
    if promoted:
        goal = json.loads(registry.read_text())["goals"][0]
        fields = parse_active_state_todos(state.read_text(), goal=goal, item_limit=None)
        projection = build_todo_runtime_shadow_projection(
            goal_id=GOAL_ID, todos=fields["agent_todos"]["items"], handoff_mode="soft_claim",
        )
        initialize_canonical_authority(runtime, GOAL_ID, projection, state_path=state)
        state.unlink()
    return registry, runtime


def _api(registry: Path, runtime: Path) -> dict:
    return {"registry_path": registry, "goal_id": GOAL_ID, "runtime_root_arg": str(runtime)}


def _todo(registry: Path, runtime: Path, todo_id: str) -> dict:
    listed = list_goal_todos(**_api(registry, runtime), todo_id=todo_id)
    assert listed["todo"] is not None, listed
    return listed["todo"]


def _deliver(registry: Path, runtime: Path, todo_id: str = "todo_orders_api", **extra) -> dict:
    return complete_goal_todo(
        **_api(registry, runtime), todo_id=todo_id, role="agent",
        agent_id=DEV, evidence="endpoint implemented", **extra,
    )


# --- delivery ----------------------------------------------------------------


@pytest.mark.parametrize("promoted", [False, True])
def test_delivery_moves_to_in_review_and_accept_completes(tmp_path: Path, promoted: bool) -> None:
    registry, runtime = _setup(tmp_path, promoted=promoted)
    delivered = _deliver(registry, runtime)
    assert delivered["in_review"] is True and delivered["completed"] is False
    assert delivered["acceptance"]["transition"] == "delivered"
    assert delivered["acceptance"]["acceptor"]["agent_id"] == ACC
    todo = _todo(registry, runtime, "todo_orders_api")
    assert todo["status"] == "in_review"
    assert todo["done"] is False
    assert todo["delivered_by"] == DEV
    assert todo["claimed_by"] == DEV
    assert "delivered_by=opus-dev" in todo["evidence"]

    # A second developer completion cannot bypass the acceptor.
    with pytest.raises(ValueError, match="in_review"):
        _deliver(registry, runtime)

    accepted = accept_goal_todo(
        **_api(registry, runtime), todo_id="todo_orders_api", agent_id=ACC,
        note="meets the acceptance criteria",
    )
    assert accepted["acceptance"]["verdict"] == "accept"
    todo = _todo(registry, runtime, "todo_orders_api")
    assert todo["status"] == "done" and todo["done"] is True
    assert "accepted_by=codex-acceptor" in todo["evidence"]


@pytest.mark.parametrize("promoted", [False, True])
def test_todo_without_acceptance_goes_straight_to_done(tmp_path: Path, promoted: bool) -> None:
    registry, runtime = _setup(tmp_path, promoted=promoted)
    result = _deliver(registry, runtime, "todo_orders_notes")
    assert result.get("in_review") is None
    assert result["completed"] is True
    assert _todo(registry, runtime, "todo_orders_notes")["status"] == "done"


def test_failed_validation_keeps_the_todo_open(tmp_path: Path) -> None:
    registry, runtime = _setup(tmp_path, promoted=False)
    added = add_goal_todo(
        **_api(registry, runtime), role="agent", text="Implement the failing check.",
        task_class="advancement_task", claimed_by=DEV,
        validation_command_json=json.dumps([sys.executable, "-c", "raise SystemExit(3)"]),
    )
    result = _deliver(registry, runtime, added["todo_id"])
    assert result["ok"] is False and result["validation_blocked_completion"] is True
    assert _todo(registry, runtime, added["todo_id"])["status"] == "open"
    added = add_goal_todo(
        **_api(registry, runtime), role="agent", text="Implement the passing check.",
        task_class="advancement_task", claimed_by=DEV,
        validation_command_json=json.dumps([sys.executable, "-c", "pass"]),
    )
    result = _deliver(registry, runtime, added["todo_id"])
    assert result["in_review"] is True
    assert result["acceptance"]["validation"]["passed"] is True
    assert "validation=passed" in _todo(registry, runtime, added["todo_id"])["evidence"]


# --- verdicts ----------------------------------------------------------------


@pytest.mark.parametrize("promoted", [False, True])
def test_reject_reopens_for_the_same_developer_then_escalates(tmp_path: Path, promoted: bool) -> None:
    registry, runtime = _setup(tmp_path, promoted=promoted)
    _deliver(registry, runtime)
    rejected = reject_goal_todo(
        **_api(registry, runtime), todo_id="todo_orders_api", agent_id=ACC,
        note="pagination is missing",
    )
    assert rejected["acceptance"]["transition"] == "reopened"
    todo = _todo(registry, runtime, "todo_orders_api")
    assert todo["status"] == "open"
    assert todo["claimed_by"] == DEV
    assert todo["reject_count"] == 1
    assert "pagination is missing" in todo["review_feedback"]

    # The developer's next selection carries the feedback.
    developer = _quota_summary(registry, runtime, DEV)
    item = next(i for i in developer["first_executable_items"] if i["todo_id"] == "todo_orders_api")
    assert "pagination is missing" in item["review_feedback"]

    _deliver(registry, runtime)
    escalated = reject_goal_todo(
        **_api(registry, runtime), todo_id="todo_orders_api", agent_id=ACC,
        note="pagination is still missing",
    )
    acceptance = escalated["acceptance"]
    assert acceptance["transition"] == "escalated"
    assert acceptance["reject_count"] == 2
    assert acceptance["escalation"]["routed_to"] == ORCH
    todo = _todo(registry, runtime, "todo_orders_api")
    assert todo["status"] == "blocked"
    assert todo["claimed_by"] == DEV  # never auto-reassigned
    assert todo["reject_count"] == 2
    escalation = _todo(registry, runtime, acceptance["escalation"]["todo_id"])
    assert escalation["required_role"] == "orchestrator"
    assert escalation["action_kind"] == "replan"
    assert escalation["claimed_by"] == ORCH
    # The orchestrator receives the escalation; the developer does not.
    orch = _quota_summary(registry, runtime, ORCH)
    assert acceptance["escalation"]["todo_id"] in [
        i["todo_id"] for i in orch["first_executable_items"]
    ]
    assert "todo_orders_api" not in [
        i["todo_id"] for i in _quota_summary(registry, runtime, DEV)["first_executable_items"]
    ]


@pytest.mark.parametrize("promoted", [False, True])
def test_only_the_resolved_acceptor_can_record_a_verdict(tmp_path: Path, promoted: bool) -> None:
    registry, runtime = _setup(tmp_path, promoted=promoted)
    with pytest.raises(ValueError, match="only in_review"):
        accept_goal_todo(**_api(registry, runtime), todo_id="todo_orders_api", agent_id=ACC)
    _deliver(registry, runtime)
    for wrong in (DEV, ORCH):
        with pytest.raises(ValueError, match="its acceptor is 'codex-acceptor'"):
            accept_goal_todo(**_api(registry, runtime), todo_id="todo_orders_api", agent_id=wrong)
        with pytest.raises(ValueError, match="its acceptor is 'codex-acceptor'"):
            reject_goal_todo(
                **_api(registry, runtime), todo_id="todo_orders_api", agent_id=wrong, note="no",
            )
    with pytest.raises(ValueError, match="requires --note"):
        reject_goal_todo(**_api(registry, runtime), todo_id="todo_orders_api", agent_id=ACC, note="")
    assert _todo(registry, runtime, "todo_orders_api")["status"] == "in_review"


def test_several_acceptors_require_a_bound_acceptor(tmp_path: Path) -> None:
    registry, runtime = _setup(tmp_path, promoted=False, acceptors={"codex-acc-2": "acceptor"})
    _deliver(registry, runtime)
    with pytest.raises(ValueError, match="orchestrator must bind one"):
        accept_goal_todo(**_api(registry, runtime), todo_id="todo_orders_api", agent_id=ACC)
    # Unresolved review is routed to the orchestrator.
    orch = _quota_summary(registry, runtime, ORCH)
    assert "todo_orders_api" in [i["todo_id"] for i in orch["first_executable_items"]]
    assert todo_review_agent({"acceptor_agent": "codex-acc-2"}, [ACC, "codex-acc-2"]) == "codex-acc-2"
    assert todo_review_agent({}, [ACC, "codex-acc-2"]) is None
    assert todo_review_agent({}, [ACC]) == ACC


# --- selection ---------------------------------------------------------------


def _quota_summary(registry: Path, runtime: Path, agent: str) -> dict:
    goal = json.loads(registry.read_text())["goals"][0]
    listed = list_goal_todos(**_api(registry, runtime), role="agent")
    identity = build_quota_agent_identity(goal, agent_id=agent)
    return select_quota_todo_summary(listed["agent_todos"], None, agent_identity=identity)


@pytest.mark.parametrize("promoted", [False, True])
def test_in_review_selection_is_role_aware(tmp_path: Path, promoted: bool) -> None:
    registry, runtime = _setup(tmp_path, promoted=promoted)
    _deliver(registry, runtime)

    def executable(agent: str) -> list[str]:
        return [i["todo_id"] for i in _quota_summary(registry, runtime, agent)["first_executable_items"]]

    assert executable(ACC) == ["todo_orders_api"]
    assert "todo_orders_api" not in executable(DEV)
    assert "todo_orders_api" not in executable(ORCH)
    summary = _quota_summary(registry, runtime, ACC)
    assert summary["role_scope"]["review_open_count"] == 1


def _gate_summary(global_gate: bool, agent: str) -> dict:
    goal = {"id": GOAL_ID, "coordination": {"agent_model": "role_v1",
            "registered_agents": [ORCH, DEV, ACC], "agent_roles": ROLES}}
    gate = {"todo_id": "todo_gate_scope", "text": "Approve the orders schema.", "status": "open",
            "role": "user", "task_class": "user_gate", "done": False, "index": 1}
    if global_gate:
        gate["global_gate"] = True
    identity = build_quota_agent_identity(goal, agent_id=agent)
    return summarize_user_todos_for_quota(
        {"schema_version": "todo_summary_v0", "items": [gate], "total_count": 1, "open_count": 1},
        agent_identity=identity, filter_user_gate_blocks_agent=True,
    )


def test_user_gate_blocking_is_role_aware() -> None:
    # An unscoped gate blocks developers but not the independent acceptor.
    assert _gate_summary(False, DEV)["open_count"] == 1
    assert _gate_summary(False, ACC)["open_count"] == 0
    # A global gate stops every lane, the acceptor's included.
    assert _gate_summary(True, ACC)["open_count"] == 1


# --- projection and replay ---------------------------------------------------


def test_status_projection_and_todo_list_show_in_review(tmp_path: Path) -> None:
    from loopx.control_plane.testing.canary_harness import run_json_cli_result

    registry, runtime = _setup(tmp_path, promoted=False)
    code, delivered = run_json_cli_result(
        "todo", "complete", "--goal-id", GOAL_ID, "--todo-id", "todo_orders_api",
        "--agent-id", DEV, "--evidence", "endpoint implemented",
        registry_path=registry, runtime_root=runtime,
    )
    assert code == 0, delivered
    assert delivered["in_review"] is True
    code, listed = run_json_cli_result(
        "todo", "list", "--goal-id", GOAL_ID, "--status", "in_review",
        registry_path=registry, runtime_root=runtime,
    )
    assert code == 0, listed
    assert [item["todo_id"] for item in listed["todos"]] == ["todo_orders_api"]
    state = (tmp_path / "state.md").read_text(encoding="utf-8")
    assert "status=in_review" in state and "delivered_by=opus-dev" in state
    code, status = run_json_cli_result(
        "status", "--goal-id", GOAL_ID, registry_path=registry, runtime_root=runtime,
    )
    assert code == 0, status
    assert "in_review" in json.dumps(status)

    code, rejected = run_json_cli_result(
        "todo", "reject", "--goal-id", GOAL_ID, "--todo-id", "todo_orders_api",
        "--agent-id", ACC, "--note", "add pagination",
        registry_path=registry, runtime_root=runtime,
    )
    assert code == 0, rejected
    assert rejected["acceptance"]["reject_count"] == 1
    code, wrong = run_json_cli_result(
        "todo", "accept", "--goal-id", GOAL_ID, "--todo-id", "todo_orders_api",
        "--agent-id", DEV, registry_path=registry, runtime_root=runtime,
    )
    assert code != 0 and wrong["ok"] is False
    run_json_cli_result(
        "todo", "complete", "--goal-id", GOAL_ID, "--todo-id", "todo_orders_api",
        "--agent-id", DEV, registry_path=registry, runtime_root=runtime,
    )
    code, accepted = run_json_cli_result(
        "todo", "accept", "--goal-id", GOAL_ID, "--todo-id", "todo_orders_api",
        "--agent-id", ACC, "--note", "pagination added",
        registry_path=registry, runtime_root=runtime,
    )
    assert code == 0, accepted
    assert accepted["acceptance"]["verdict"] == "accept"


def test_event_replay_projects_in_review_and_reopen() -> None:
    from loopx.event_sourced_state import build_state_projection, make_state_event

    def event(kind: str, sequence: int, payload: dict) -> dict:
        return make_state_event(
            event_id=f"evt-{sequence}", goal_id=GOAL_ID, event_type=kind,
            refs={"todo_id": "todo_replayed"}, payload=payload,
            recorded_at=f"2026-09-26T00:00:0{sequence}Z", producer="test",
        )

    events = [
        event("todo_added", 1, {"text": "Implement replay.", "role": "agent"}),
        event("todo_in_review", 2, {"delivered_by": DEV, "evidence": "delivered"}),
    ]
    projection = build_state_projection(events, goal_id=GOAL_ID)
    item = projection["agent_todos"]["items"][0]
    assert item["status"] == "in_review" and item["done"] is False
    assert item["delivered_by"] == DEV
    assert projection["agent_todos"]["open_count"] == 1
    events.append(event("todo_reopened", 3, {"review_feedback": "missing tests", "reject_count": 1}))
    item = build_state_projection(events, goal_id=GOAL_ID)["agent_todos"]["items"][0]
    assert item["status"] == "open" and item["reject_count"] == 1
    events.append(event("todo_in_review", 4, {"delivered_by": DEV}))
    events.append(event("todo_completed", 5, {"evidence": "accepted"}))
    item = build_state_projection(events, goal_id=GOAL_ID)["agent_todos"]["items"][0]
    assert item["status"] == "done" and item["done"] is True
