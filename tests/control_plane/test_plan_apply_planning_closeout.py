"""Decision 43: an approved plan card closes the orchestrator planning todo on every authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopx.plan_cards import propose_plan
from loopx.todos import add_goal_todo, complete_goal_todo, list_goal_todos
from tests.control_plane.test_gates_plans_intake import GOAL, ORCH, PLAN, fixture


@pytest.mark.parametrize("provider", [None, "file", "sqlite"])
def test_plan_approval_completes_the_planning_todo_with_plan_evidence(tmp_path: Path, provider: str | None) -> None:
    registry, runtime = fixture(tmp_path, provider)
    planning = add_goal_todo(
        registry_path=registry, goal_id=GOAL, role="agent", text="Clarify, then propose the plan",
        action_kind="plan", claimed_by=ORCH, agent_id=ORCH,
        role_contract={"required_role": "orchestrator", "requires_acceptance": False},
    )["todo_id"]
    record = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH, plan=PLAN)["plan"]
    rows = {row["todo_id"]: row for row in list_goal_todos(registry_path=registry, goal_id=GOAL)["todos"]}
    assert rows[planning]["status"] == "open"  # proposing is not enough

    decided = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=record["gate_todo_id"], role="user",
                                 decision_outcome="approve", agent_id=ORCH)
    [closed] = decided["plan_card"]["planning_todos_closed"]
    assert closed == {**closed, "todo_id": planning, "closed": True, "kind": "planning_closeout"}
    row = {row["todo_id"]: row for row in list_goal_todos(registry_path=registry, goal_id=GOAL)["todos"]}[planning]
    assert row["status"] == "done"
    assert f"plan_applied={record['plan_id']}" in row["evidence"]
    assert f"created_todos={len(PLAN['todos'])}" in row["evidence"]


def test_a_rejected_plan_leaves_the_planning_todo_open(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    planning = add_goal_todo(
        registry_path=registry, goal_id=GOAL, role="agent", text="Clarify, then propose the plan",
        action_kind="plan", claimed_by=ORCH,
        role_contract={"required_role": "orchestrator", "requires_acceptance": False},
    )["todo_id"]
    record = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH, plan=PLAN)["plan"]
    decided = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=record["gate_todo_id"], role="user",
                                 decision_outcome="reject", agent_id=ORCH)
    assert "planning_todos_closed" not in decided["plan_card"]
    row = {row["todo_id"]: row for row in list_goal_todos(registry_path=registry, goal_id=GOAL)["todos"]}[planning]
    assert row["status"] == "open"
