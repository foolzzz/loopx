"""Pilot v1 gap N8: ``plan propose --revise`` refreshes the plan gate's text.

The gate kept the rev-1 title ("(5 todos)") after a revision to a 4-todo card.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_gates_plans_intake import GOAL, ORCH, PLAN, fixture, rows

from loopx.plan_cards import propose_plan
from loopx.todos import complete_goal_todo


@pytest.mark.parametrize("provider", [None, "file", "sqlite"])
def test_revise_rewrites_the_open_gate_title_and_note(tmp_path: Path, provider: str | None, monkeypatch) -> None:
    if provider == "sqlite":
        from canonical_authority_fixture import isolate_sqlite_runtime
        isolate_sqlite_runtime(tmp_path, monkeypatch)
    registry, runtime = fixture(tmp_path, provider)
    plan = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH, plan=PLAN)["plan"]
    gate_id, plan_id = plan["gate_todo_id"], plan["plan_id"]
    assert rows(registry)[gate_id]["text"] == f"Approve plan: Todo app v1 (5 todos) [{plan_id}]"

    revised_plan = {**PLAN, "title": "Todo app v1 (no README)", "summary": "Contract, API, web, integration.",
                    "todos": PLAN["todos"][:4]}
    revised = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH,
                           plan=revised_plan, revise_plan_id=plan_id)["plan"]
    assert revised["revision"] == 2 and revised["gate_todo_id"] == gate_id
    gate = rows(registry)[gate_id]
    assert gate["text"] == f"Approve plan: Todo app v1 (no README) (4 todos) [{plan_id}]"
    assert gate["note"] == "Contract, API, web, integration."
    assert (gate["status"], gate["task_class"]) == ("open", "user_gate")

    # The refreshed gate still approves the revised card.
    done = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=gate_id, role="user",
                              decision_outcome="approve", note="ok", no_followup=True, agent_id=ORCH)
    assert list(done["plan_card"]["todo_id_map"]) == ["contract", "api", "web", "integrate"]
