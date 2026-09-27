"""Compact per-goal usage summary for the status payload (fork gap G9).

``run_history.goals[].turn_usage_summary`` is attached to every goal that has a
usage ledger (``goals/<goal>/usage.jsonl``), role_v1 or not: the numbers are
neutral facts about spend. The read is bounded like the role board: only the
most recent ``MAX_TURN_USAGE_SUMMARY_ENTRIES`` ledger rows are aggregated and the
role split keeps at most ``MAX_TURN_USAGE_SUMMARY_ROLES`` rows. Accepted todos come
from the same status ``todo_index`` the role board reads. A missing or broken
ledger leaves the goal without a summary; it never fails the status read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

TURN_USAGE_SUMMARY_SCHEMA_VERSION = "loopx_turn_usage_summary_v0"
MAX_TURN_USAGE_SUMMARY_ENTRIES = 5000
MAX_TURN_USAGE_SUMMARY_ROLES = 8


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def build_goal_usage_summary(
    *, goal_id: str, runtime_root: Path, todos: list[Mapping[str, Any]]
) -> dict[str, Any] | None:
    from ...usage_accounting.budget import budget_status
    from ...usage_accounting.ledger import read_usage_entries, usage_ledger_path
    from ...usage_accounting.report import aggregate_usage, accepted_todo_keys_from_items

    if not usage_ledger_path(runtime_root, goal_id).is_file():
        return None
    entries = read_usage_entries(runtime_root, goal_id, limit=MAX_TURN_USAGE_SUMMARY_ENTRIES + 1)
    truncated = len(entries) > MAX_TURN_USAGE_SUMMARY_ENTRIES
    entries = entries[-MAX_TURN_USAGE_SUMMARY_ENTRIES:]
    if not entries:
        return None
    accepted = accepted_todo_keys_from_items(goal_id, todos)
    report = aggregate_usage(entries, by="role", accepted_todo_keys=accepted)
    totals = report["totals"]
    summary: dict[str, Any] = {
        "schema_version": TURN_USAGE_SUMMARY_SCHEMA_VERSION,
        "turns": totals["turns"],
        "failed_turns": totals["failed_turns"],
        "agent_hours": totals["agent_hours"],
        "tokens_total": totals["tokens"]["total"],
        "cost_usd": totals["cost_usd"],
        "cost_estimated_usd": totals["cost_estimated_usd"],
        "unpriced_turns": totals["unpriced_turns"],
        "accepted_todos": totals.get("accepted_todos", 0),
        "cost_per_accepted_todo_usd": totals.get("cost_per_accepted_todo_usd"),
        "turns_per_accepted_todo": totals.get("turns_per_accepted_todo"),
        "last_turn_at": report.get("last_turn_at"),
        "by_role": [
            {
                "role": group["key"],
                "turns": group["turns"],
                "agent_hours": group["agent_hours"],
                "cost_usd": group["cost_usd"],
                "cost_estimated_usd": group["cost_estimated_usd"],
            }
            for group in report.get("groups", [])[:MAX_TURN_USAGE_SUMMARY_ROLES]
        ],
    }
    budget = budget_status(runtime_root, goal_id, spent_usd=totals["cost_usd"])
    if budget is not None:
        summary["budget"] = {
            "budget_usd": budget["budget_usd"],
            "spent_ratio": budget["spent_ratio"],
        }
    if truncated:
        summary["truncated"] = True
    return summary


def attach_goal_usage_summaries(payload: dict[str, Any], *, runtime_root: Path) -> None:
    """Attach ``run_history.goals[].turn_usage_summary`` in place."""

    history = payload.get("run_history")
    goals = _rows(history.get("goals")) if isinstance(history, dict) else []
    if not goals:
        return
    index = payload.get("todo_index")
    todos_by_goal: dict[str, list[dict[str, Any]]] = {}
    for item in _rows(index.get("items") if isinstance(index, dict) else None):
        todos_by_goal.setdefault(str(item.get("goal_id") or ""), []).append(item)
    for goal in goals:
        goal_id = str(goal.get("id") or "")
        if not goal_id:
            continue
        try:
            summary = build_goal_usage_summary(
                goal_id=goal_id, runtime_root=runtime_root, todos=todos_by_goal.get(goal_id, []),
            )
        except Exception:  # noqa: BLE001 - usage is a read model; never fail status
            summary = None
        if summary is not None:
            goal["turn_usage_summary"] = summary
