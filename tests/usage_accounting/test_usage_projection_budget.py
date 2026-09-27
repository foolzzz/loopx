"""G9: the bounded per-goal usage summary in status, and the dispatcher budget alert."""

from __future__ import annotations

import sys
from pathlib import Path

from loopx.control_plane.status.usage_projection import (
    TURN_USAGE_SUMMARY_SCHEMA_VERSION,
    attach_goal_usage_summaries,
    build_goal_usage_summary,
)
from loopx.dispatch import DispatchConfig, Dispatcher
from loopx.status import collect_status
from loopx.usage_accounting import record_turn_usage, write_usage_budget
from tests.dispatch.dispatch_fixtures import GOAL_ID, write_fixture
from tests.dispatch.test_loopx_dispatcher import Clock, ScriptedShouldRun, _user_gates
from tests.usage_accounting.test_usage_ledger_report import _claude_usage


def _record(runtime: Path, goal: str, key: str, *, role: str, todo: str, seconds: float = 1800.0) -> None:
    record_turn_usage(runtime, goal, _claude_usage(seconds=seconds), agent_id=f"{role}-1", role=role,
                      todo_id=todo, turn_key="sha256:" + key * 64, status="committed")


def test_usage_summary_is_bounded_and_splits_roles(tmp_path: Path) -> None:
    runtime = tmp_path
    _record(runtime, "g", "1", role="developer", todo="todo_a1")
    _record(runtime, "g", "2", role="acceptor", todo="todo_a1", seconds=900)
    _record(runtime, "g", "3", role="developer", todo="todo_b2")
    write_usage_budget(runtime, "g", 1.0)
    todos = [{"todo_id": "todo_a1", "status": "done"}, {"todo_id": "todo_b2", "status": "open"}]

    summary = build_goal_usage_summary(goal_id="g", runtime_root=runtime, todos=todos)
    assert summary["schema_version"] == TURN_USAGE_SUMMARY_SCHEMA_VERSION
    assert summary["turns"] == 3 and summary["accepted_todos"] == 1
    assert summary["agent_hours"] == 1.25
    assert summary["cost_per_accepted_todo_usd"] == summary["cost_usd"]
    assert [row["role"] for row in summary["by_role"]] == ["developer", "acceptor"]
    assert summary["budget"]["budget_usd"] == 1.0 and 0.1 < summary["budget"]["spent_ratio"] < 0.2
    assert build_goal_usage_summary(goal_id="empty", runtime_root=runtime, todos=[]) is None

    payload = {"run_history": {"goals": [{"id": "g"}, {"id": "empty"}]},
               "todo_index": {"items": [{"goal_id": "g", **todo} for todo in todos]}}
    attach_goal_usage_summaries(payload, runtime_root=runtime)
    goals = payload["run_history"]["goals"]
    assert goals[0]["turn_usage_summary"]["turns"] == 3 and "turn_usage_summary" not in goals[1]
    # A torn ledger line degrades to the rows that parse; status never fails.
    with (runtime / "goals" / "g" / "usage.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{torn\n")
    attach_goal_usage_summaries(payload, runtime_root=runtime)
    assert goals[0]["turn_usage_summary"]["turns"] == 3


def test_status_projection_carries_the_usage_summary(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    _record(fixture["runtime"], GOAL_ID, "9", role="developer", todo="todo_x1")
    payload = collect_status(registry_path=fixture["registry"], runtime_root_override=str(fixture["runtime"]),
                             scan_roots=[], limit=5)
    goal = next(goal for goal in payload["run_history"]["goals"] if goal["id"] == GOAL_ID)
    assert goal["turn_usage_summary"]["turns"] == 1
    assert goal["turn_usage_summary"]["by_role"][0]["role"] == "developer"


def test_dispatcher_opens_one_non_blocking_alert_per_budget_threshold(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    runtime = fixture["runtime"]
    _record(runtime, GOAL_ID, "1", role="developer", todo="todo_x1")  # ~$0.047
    write_usage_budget(runtime, GOAL_ID, 0.05)
    clock = Clock()

    def dispatcher() -> Dispatcher:
        config = DispatchConfig(
            registry_path=fixture["registry"], runtime_root=runtime, goal_ids=[GOAL_ID],
            no_global_sync=True, environ=fixture["environ"],
            loopx_argv=(sys.executable, str(fixture["fake_loopx"])),
        )
        return Dispatcher(config, should_run=ScriptedShouldRun({"dev": []}), clock=clock)

    first = dispatcher().reconcile()
    assert first["errors"] == []
    assert [item["key"] for item in first["gates_opened"]] == ["usage_budget:80"]
    alerts = _user_gates(fixture)
    assert len(alerts) == 1 and alerts[0]["task_class"] == "user_action"
    assert "Usage budget 80% reached" in alerts[0]["text"] and "Agents keep running" in alerts[0]["text"]
    assert not alerts[0].get("blocks_agent")

    same = dispatcher()
    assert same.reconcile()["gates_opened"] == []

    _record(runtime, GOAL_ID, "2", role="developer", todo="todo_x1")  # now above 100%
    second = dispatcher().reconcile()
    assert [item["key"] for item in second["gates_opened"]] == ["usage_budget:100"]
    assert dispatcher().reconcile()["gates_opened"] == []
    assert len(_user_gates(fixture)) == 2
