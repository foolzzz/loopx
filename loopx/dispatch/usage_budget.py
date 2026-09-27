"""Usage budget alerts (fork gap G9, design decision 41).

When a goal has a budget (``loopx usage budget --set``) and its recorded
spend crosses 80% of it, the dispatcher opens one user todo per budget value.
The todo is a ``user_action``, which never blocks an agent lane: the 80%
alert informs the user, it does not stop work. The alert memory lives in the
dispatcher state (``budget_alerts``); an open alert todo is also adopted by
its text, so a lost state file does not duplicate an open alert.

At 100% the dispatcher opens a ``budget_exhausted`` user gate instead
(``loopx.usage_budget_gate``), which pauses the goal's new Turns until the
owner raises the budget, drops the limit or stops the goal.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..usage_accounting.budget import budget_status
from ..usage_budget_gate import open_budget_gate

USAGE_BUDGET_ALERT_PREFIX = "Usage budget"


def _alert_marker(goal_id: str, percent: int) -> str:
    return f"{USAGE_BUDGET_ALERT_PREFIX} {percent}% reached for goal {goal_id}"


def _open_user_texts(registry_path: Path, runtime_root: Path, goal_id: str) -> dict[str, str]:
    from ..todos import list_goal_todos

    listed = list_goal_todos(
        registry_path=registry_path, goal_id=goal_id, role="user", status="open",
        runtime_root_arg=str(runtime_root),
    )
    return {
        str(item.get("todo_id")): str(item.get("text") or "")
        for item in listed.get("todos") or []
        if isinstance(item, Mapping) and item.get("todo_id")
    }


def alert_usage_budget(
    *,
    registry_path: Path,
    runtime_root: Path,
    goal_id: str,
    state: dict[str, Any],
    report: dict[str, Any],
    now: float,
) -> None:
    status = budget_status(runtime_root, goal_id)
    if status is None or not status["crossed"]:
        return
    alerts = state.setdefault("budget_alerts", {}).setdefault(goal_id, {})
    budget = status["budget_usd"]
    if 1.0 in status["crossed"]:
        # Decision 41: 100% opens the budget gate (once per crossing; the gate
        # index remembers it), which also covers a not yet sent 80% alert.
        opened = open_budget_gate(registry_path=registry_path, runtime_root=runtime_root, goal_id=goal_id)
        gate_id = opened.get("gate_todo_id")
        if opened.get("opened"):
            report["gates_opened"].append({"goal_id": goal_id, "key": "usage_budget:100", "todo_id": gate_id})
        for threshold in status["crossed"]:
            alerts.setdefault(f"{int(threshold * 100)}@{budget:g}", {
                "todo_id": gate_id, "opened_at": now, "adopted": not opened.get("opened"),
            })
        return
    pending = [threshold for threshold in status["crossed"] if f"{int(threshold * 100)}@{budget:g}" not in alerts]
    if not pending:
        return
    percent = int(max(pending) * 100)
    marker = _alert_marker(goal_id, percent)
    open_texts = _open_user_texts(registry_path, runtime_root, goal_id)
    adopted = next((todo_id for todo_id, text in open_texts.items() if marker in text), None)
    todo_id = adopted
    if todo_id is None:
        from ..todos import add_goal_todo

        payload = add_goal_todo(
            registry_path=registry_path,
            goal_id=goal_id,
            runtime_root_arg=str(runtime_root),
            role="user",
            text=(
                f"{marker}: ${status['spent_usd']:,.2f} of ${budget:,.2f} spent. "
                "Agents keep running; review `loopx usage report --goal "
                f"{goal_id} --by role` and raise the budget or pause the goal."
            ),
            task_class="user_action",
            goal_bound=True,  # a goal-wide alert; multi-agent goals need an explicit binding
            note="Opened by the LoopX dispatcher (usage budget).",
        )
        todo_id = payload.get("todo_id")
        report["gates_opened"].append({"goal_id": goal_id, "key": f"usage_budget:{percent}", "todo_id": todo_id})
    for threshold in pending:
        alerts[f"{int(threshold * 100)}@{budget:g}"] = {"todo_id": todo_id, "opened_at": now, "adopted": bool(adopted)}
