"""Usage report over the per-goal ledgers (fork gap G9).

Totals answer "how much agent work did this drive": LoopX Turns, agent-hours
(the sum of Turn wall-clock durations, so parallel agents add up), model
turns, tokens and cost. Host-reported and estimated cost are kept apart, and
Turns with no price stay visible as ``unpriced_turns``. Cost and Turns per
accepted todo divide by the todos that are done now and had spend in the
window.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..control_plane.turn_driver.turn_usage import TURN_USAGE_TOKEN_FIELDS
from .ledger import COST_SOURCE_ESTIMATED, COST_SOURCE_HOST_REPORTED, read_usage_entries

USAGE_REPORT_SCHEMA_VERSION = "loopx_usage_report_v0"
USAGE_REPORT_GROUPINGS = ("role", "agent", "goal", "todo", "model", "day")
USAGE_REPORT_DONE_STATUSES = frozenset({"done", "completed"})
_UNKNOWN_GROUP = "unknown"


def parse_since(value: str | None, *, days: int | None = None, now: datetime | None = None) -> datetime | None:
    if value and days is not None:
        raise ValueError("use either --since or --days, not both")
    now = now or datetime.now(timezone.utc)
    if days is not None:
        if days < 1:
            raise ValueError("--days must be at least 1")
        return now - timedelta(days=days)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--since must be an ISO date or timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _started(row: Mapping[str, Any]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(row.get("started_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _day(row: Mapping[str, Any]) -> str:
    started = _started(row)
    return started.astimezone().date().isoformat() if started else _UNKNOWN_GROUP


def _group_key(row: Mapping[str, Any], by: str) -> str:
    if by == "day":
        return _day(row)
    field = {"agent": "agent_id", "goal": "goal_id", "todo": "todo_id"}.get(by, by)
    value = row.get(field)
    return str(value) if isinstance(value, str) and value else _UNKNOWN_GROUP


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


class _Totals:
    def __init__(self) -> None:
        self.turns = 0
        self.failed_turns = 0
        self.model_turns = 0
        self.duration_ms = 0
        self.tokens = {field: 0 for field in (*TURN_USAGE_TOKEN_FIELDS, "total")}
        self.cost_reported = 0.0
        self.cost_estimated = 0.0
        self.unpriced_turns = 0
        self.todo_ids: set[str] = set()

    def add(self, row: Mapping[str, Any], *, cost: float | None = None, tokens: Mapping[str, Any] | None = None) -> None:
        self.turns += 1
        if row.get("status") not in {"committed", "stopped"}:
            self.failed_turns += 1
        self.model_turns += int(_number(row.get("num_turns")))
        self.duration_ms += int(_number(row.get("duration_ms")))
        source_tokens = tokens if tokens is not None else row.get("tokens")
        if isinstance(source_tokens, Mapping):
            for field in self.tokens:
                self.tokens[field] += int(_number(source_tokens.get(field)))
        value = _number(row.get("cost_usd")) if cost is None else cost
        if row.get("cost_source") == COST_SOURCE_HOST_REPORTED:
            self.cost_reported += value
        elif row.get("cost_source") == COST_SOURCE_ESTIMATED:
            self.cost_estimated += value
        else:
            self.unpriced_turns += 1
        if isinstance(row.get("todo_id"), str) and row["todo_id"]:
            self.todo_ids.add(f"{row.get('goal_id')}/{row['todo_id']}")

    def payload(self, accepted: set[str] | None) -> dict[str, Any]:
        cost = round(self.cost_reported + self.cost_estimated, 6)
        payload: dict[str, Any] = {
            "turns": self.turns,
            "failed_turns": self.failed_turns,
            "model_turns": self.model_turns,
            "agent_hours": round(self.duration_ms / 3_600_000, 4),
            "tokens": dict(self.tokens),
            "cost_usd": cost,
            "cost_reported_usd": round(self.cost_reported, 6),
            "cost_estimated_usd": round(self.cost_estimated, 6),
            "unpriced_turns": self.unpriced_turns,
            "todos": len(self.todo_ids),
        }
        if accepted is not None:
            count = len(self.todo_ids & accepted)
            payload["accepted_todos"] = count
            payload["cost_per_accepted_todo_usd"] = round(cost / count, 6) if count else None
            payload["turns_per_accepted_todo"] = round(self.turns / count, 2) if count else None
        return payload


def aggregate_usage(
    rows: Iterable[Mapping[str, Any]],
    *,
    by: str | None = None,
    since: datetime | None = None,
    accepted_todo_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Totals and optional groups. ``accepted_todo_keys`` holds ``goal/todo`` keys."""

    if by is not None and by not in USAGE_REPORT_GROUPINGS:
        raise ValueError(f"--by must be one of {', '.join(USAGE_REPORT_GROUPINGS)}")
    totals = _Totals()
    groups: dict[str, _Totals] = {}
    first: str | None = None
    last: str | None = None
    for row in rows:
        started = _started(row)
        if since is not None and (started is None or started < since):
            continue
        totals.add(row)
        stamp = str(row.get("started_at") or "")
        first = stamp if first is None or stamp < first else first
        last = stamp if last is None or stamp > last else last
        if by is None:
            continue
        models = row.get("models") if isinstance(row.get("models"), list) else []
        if by == "model" and len(models) > 1:
            # Split a multi-model Turn by its per-model cost and tokens.
            for entry in models:
                if not isinstance(entry, Mapping):
                    continue
                key = str(entry.get("model") or _UNKNOWN_GROUP)
                groups.setdefault(key, _Totals()).add(
                    row, cost=_number(entry.get("cost_usd")), tokens=entry.get("tokens") or {}
                )
            continue
        groups.setdefault(_group_key(row, by), _Totals()).add(row)
    report: dict[str, Any] = {
        "schema_version": USAGE_REPORT_SCHEMA_VERSION,
        "since": since.isoformat(timespec="seconds") if since else None,
        "first_turn_at": first,
        "last_turn_at": last,
        "totals": totals.payload(accepted_todo_keys),
    }
    if by is not None:
        report["by"] = by
        rows_out = [{"key": key, **group.payload(accepted_todo_keys)} for key, group in groups.items()]
        rows_out.sort(key=lambda item: (-item["cost_usd"], -item["turns"], item["key"]))
        if by == "day":
            rows_out.sort(key=lambda item: item["key"])
        report["groups"] = rows_out
    return report


def accepted_todo_keys_from_items(goal_id: str, todos: Iterable[Mapping[str, Any]]) -> set[str]:
    keys = set()
    for todo in todos:
        status = str(todo.get("status") or ("done" if todo.get("done") else "")).strip().lower()
        todo_id = todo.get("todo_id")
        if status in USAGE_REPORT_DONE_STATUSES and isinstance(todo_id, str) and todo_id:
            keys.add(f"{goal_id}/{todo_id}")
    return keys


def _goal_accepted_todos(registry_path: Path, runtime_root: Path, goal_id: str) -> set[str] | None:
    try:
        from ..todos import list_goal_todos

        listed = list_goal_todos(
            registry_path=registry_path,
            goal_id=goal_id,
            role="agent",
            runtime_root_arg=str(runtime_root),
        )
    except Exception:  # noqa: BLE001 - a report still shows spend without todo state
        return None
    return accepted_todo_keys_from_items(goal_id, listed.get("todos") or [])


def registry_goal_ids(registry_path: Path) -> list[str]:
    from ..history import load_registry
    from ..registry import registry_goals

    if not Path(registry_path).exists():
        return []
    return [str(goal.get("id")) for goal in registry_goals(load_registry(registry_path)) if goal.get("id")]


def build_usage_report(
    *,
    registry_path: Path,
    runtime_root: Path,
    goal_ids: list[str] | None,
    by: str | None = None,
    since: datetime | None = None,
) -> dict[str, Any]:
    """Report one goal, several goals, or every registry goal (``goal_ids=None``)."""

    selected = goal_ids if goal_ids else registry_goal_ids(registry_path)
    rows: list[dict[str, Any]] = []
    accepted: set[str] | None = set()
    goals_with_usage: list[str] = []
    for goal_id in dict.fromkeys(selected):
        goal_rows = read_usage_entries(runtime_root, goal_id)
        if not goal_rows:
            continue
        goals_with_usage.append(goal_id)
        rows.extend(goal_rows)
        goal_accepted = _goal_accepted_todos(registry_path, runtime_root, goal_id)
        if goal_accepted is None:
            accepted = None
        elif accepted is not None:
            accepted |= goal_accepted
    report = aggregate_usage(rows, by=by, since=since, accepted_todo_keys=accepted)
    report["goal_ids"] = goals_with_usage
    report["scope"] = "goal" if goal_ids else "registry"
    return report


def _money(value: Any) -> str:
    return "-" if value is None else f"${float(value):,.2f}"


def _line(label: str, data: Mapping[str, Any]) -> str:
    estimated = data.get("cost_estimated_usd") or 0
    parts = [
        f"{label}: {_money(data.get('cost_usd'))}",
        f"(est. {_money(estimated)})" if estimated else "",
        f"{data.get('agent_hours', 0):.2f} agent-h",
        f"{data.get('turns', 0)} turns",
        f"{data.get('tokens', {}).get('total', 0):,} tokens",
    ]
    if data.get("accepted_todos"):
        parts.append(
            f"{data['accepted_todos']} accepted, {_money(data.get('cost_per_accepted_todo_usd'))}/todo, "
            f"{data.get('turns_per_accepted_todo')} turns/todo"
        )
    if data.get("unpriced_turns"):
        parts.append(f"{data['unpriced_turns']} unpriced")
    return " ".join(part for part in parts if part)


def render_usage_report_text(report: Mapping[str, Any]) -> str:
    goals = ", ".join(report.get("goal_ids") or []) or "none"
    lines = [f"Usage report ({report.get('scope')}: {goals})"]
    if report.get("since"):
        lines.append(f"since {report['since']}")
    lines.append(_line("total", report.get("totals") or {}))
    for group in report.get("groups") or []:
        lines.append("  " + _line(f"{report.get('by')}={group['key']}", group))
    return "\n".join(lines) + "\n"
