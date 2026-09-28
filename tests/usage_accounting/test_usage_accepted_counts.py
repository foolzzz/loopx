"""Pilot v1 gap N5: "accepted" means an accept record, and orchestrator spend stands apart."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control_plane"))

from test_gates_plans_intake import DEV, GOAL, ORCH, fixture  # noqa: E402

from loopx.control_plane.status.usage_projection import build_goal_usage_summary  # noqa: E402
from loopx.todos import add_goal_todo, complete_goal_todo  # noqa: E402
from loopx.usage_accounting import record_turn_usage  # noqa: E402
from loopx.usage_accounting.report import (  # noqa: E402
    accepted_todo_keys_from_items,
    build_usage_report,
    render_usage_report_text,
)
from tests.usage_accounting.test_usage_ledger_report import _claude_usage  # noqa: E402


def _record(runtime: Path, key: str, *, role: str, todo: str) -> None:
    record_turn_usage(runtime, GOAL, _claude_usage(), agent_id=f"{role}-1", role=role, todo_id=todo,
                      turn_key="sha256:" + key * 64, status="committed")


def test_only_todos_with_an_accept_record_count_as_accepted() -> None:
    todos = [
        {"todo_id": "todo_acc", "status": "done", "evidence": "accepted_by=acc: merged"},
        {"todo_id": "todo_owner", "status": "done", "evidence": "accepted_by=owner; manual accept"},
        {"todo_id": "todo_plan", "status": "done", "evidence": "LoopX Turn validated completion: plan"},
        {"todo_id": "todo_retired", "status": "done", "note": "superseded"},
        {"todo_id": "todo_review", "status": "in_review", "evidence": "accepted_by=acc: stale"},
    ]
    assert accepted_todo_keys_from_items("g", todos) == {"g/todo_acc", "g/todo_owner"}


def test_report_and_status_summary_split_orchestrator_spend(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path, agent_model="peer_v1")
    planning = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Plan the work",
                             claimed_by=ORCH)["todo_id"]
    feature = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Build the feature",
                            claimed_by=DEV)["todo_id"]
    assert complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=planning, role="agent",
                              agent_id=ORCH, evidence="plan applied")["ok"]
    assert complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=feature, role="agent",
                              agent_id=DEV, evidence="accepted_by=acc: criteria met")["ok"]
    _record(runtime, "1", role="orchestrator", todo=planning)
    _record(runtime, "2", role="orchestrator", todo=planning)
    _record(runtime, "3", role="developer", todo=feature)
    _record(runtime, "4", role="acceptor", todo=feature)

    report = build_usage_report(registry_path=registry, runtime_root=runtime, goal_ids=[GOAL])
    totals = report["totals"]
    per_turn = totals["cost_usd"] / 4
    # Both todos are done, but only the feature has an accept record.
    assert (totals["todos"], totals["accepted_todos"]) == (2, 1)
    assert totals["orchestrator_turns"] == 2
    assert totals["orchestrator_cost_usd"] == pytest.approx(2 * per_turn, abs=1e-5)
    assert totals["cost_per_accepted_todo_usd"] == pytest.approx(totals["cost_usd"], abs=1e-5)
    assert totals["cost_per_accepted_todo_excl_orchestrator_usd"] == pytest.approx(2 * per_turn, abs=1e-5)
    text = render_usage_report_text(report)
    assert "1 accepted" in text and "without orchestrator" in text and "orchestrator $" in text

    items = [{"todo_id": planning, "status": "done", "evidence": "plan applied"},
             {"todo_id": feature, "status": "done", "evidence": "accepted_by=acc: criteria met"}]
    summary = build_goal_usage_summary(goal_id=GOAL, runtime_root=runtime, todos=items)
    assert summary["accepted_todos"] == 1
    assert summary["orchestrator_turns"] == 2
    assert summary["orchestrator_cost_usd"] == pytest.approx(totals["orchestrator_cost_usd"])
    assert summary["cost_per_accepted_todo_excl_orchestrator_usd"] == pytest.approx(2 * per_turn, abs=1e-5)
