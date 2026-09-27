"""Append-only per-goal usage ledger (fork gap G9).

Layout under the runtime root::

    goals/<goal>/usage.jsonl    one row per host attempt of a Turn

The kernel's quota spend counts scheduler slots (always 1 per Turn) and has
no tokens or money, so usage gets its own ledger instead of widening that
receipt. A row is keyed by ``entry_id = <turn_key>#<host_attempt>``: replaying
or resuming a settled Turn re-reads the same journaled ``turn_usage`` and is a
no-op here, while a genuine retry that invoked the host again is a new
attempt and is counted, because it spent again.

Rows carry ids, role, model, provider, outcome, token counts, cost and
timestamps only. No prompt, result text, session id or credential is stored.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agent_config.pricing import ProviderPricing, estimate_cost_usd
from ..control_plane.turn_driver.turn_usage import (
    TURN_USAGE_TOKEN_FIELDS,
    normalize_turn_usage,
    primary_model,
)
from ..file_lock import exclusive_file_lock
from ..history import validate_goal_id_path_segment

USAGE_LEDGER_FILENAME = "usage.jsonl"
USAGE_LEDGER_ENTRY_SCHEMA = "loopx_usage_ledger_entry_v0"
COST_SOURCE_HOST_REPORTED = "host_reported"
COST_SOURCE_ESTIMATED = "estimated"
COST_SOURCE_UNPRICED = "unpriced"
COST_SOURCES = (COST_SOURCE_HOST_REPORTED, COST_SOURCE_ESTIMATED, COST_SOURCE_UNPRICED)
_MAX_ID_CHARS = 160


def usage_ledger_path(runtime_root: Path, goal_id: str) -> Path:
    return (
        Path(runtime_root).expanduser()
        / "goals"
        / validate_goal_id_path_segment(goal_id)
        / USAGE_LEDGER_FILENAME
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_ID_CHARS or any(ch in text for ch in "\x00\r\n"):
        return None
    return text


def read_usage_entries(
    runtime_root: Path, goal_id: str, *, limit: int | None = None
) -> list[dict[str, Any]]:
    """Ledger rows in append order; torn or foreign lines are skipped.

    ``limit`` keeps only the most recent rows (bounded read models).
    """

    path = usage_ledger_path(runtime_root, goal_id)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    if limit is not None:
        lines = lines[-limit:]
    rows: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("schema_version") == USAGE_LEDGER_ENTRY_SCHEMA:
            rows.append(row)
    return rows


def _session_delta(
    tokens: Mapping[str, int] | None,
    usage: Mapping[str, Any],
    previous_rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, int] | None, dict[str, Any]]:
    """Per-Turn tokens from a host's cumulative session totals."""

    if tokens is None or usage.get("cumulative") is not True:
        return (dict(tokens) if tokens else None), {}
    ref = usage.get("session_ref")
    previous = None
    for row in previous_rows:
        if row.get("session_ref") == ref and isinstance(row.get("session_cumulative_tokens"), Mapping):
            previous = row["session_cumulative_tokens"]
    extra: dict[str, Any] = {"session_cumulative_tokens": dict(tokens)}
    if previous is not None and all(
        int(tokens.get(field, 0)) >= int(previous.get(field, 0) or 0) for field in TURN_USAGE_TOKEN_FIELDS
    ):
        delta = {
            field: int(tokens.get(field, 0)) - int(previous.get(field, 0) or 0)
            for field in TURN_USAGE_TOKEN_FIELDS
        }
        delta["total"] = (
            delta["input"] + delta["cached_input"] + delta["cache_creation_input"] + delta["output"]
        )
        return delta, extra
    if usage.get("session_resumed") is True:
        # A resumed session whose previous Turn was never recorded: the
        # totals include earlier Turns, so they over-count. Say so.
        extra["cumulative_unresolved"] = True
    return dict(tokens), extra


def build_usage_entry(
    turn_usage: Mapping[str, Any],
    *,
    goal_id: str,
    agent_id: str,
    turn_key: str,
    role: str | None = None,
    todo_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    outcome: str | None = None,
    status: str | None = None,
    failure_kind: str | None = None,
    pricing: ProviderPricing | None = None,
    previous_rows: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """One ledger row from a journaled ``turn_usage`` block and its Turn lineage."""

    usage = normalize_turn_usage(turn_usage)
    attempt = int(usage.get("host_attempt") or 1)
    key = _token(turn_key)
    if key is None:
        raise ValueError("usage entry needs a turn key")
    tokens, extra = _session_delta(usage.get("tokens"), usage, previous_rows)
    primary = primary_model(usage) or _token(model)
    cost = usage.get("cost_usd")
    if cost is not None:
        cost_source = COST_SOURCE_HOST_REPORTED
    else:
        rates = pricing.rates_for(primary) if pricing is not None else None
        cost = estimate_cost_usd(tokens, rates)
        cost_source = COST_SOURCE_ESTIMATED if cost is not None else COST_SOURCE_UNPRICED
    models = []
    for entry in usage.get("models") or []:
        entry_tokens = entry.get("tokens")
        entry_cost = entry.get("cost_usd")
        if len(usage.get("models") or []) == 1:
            entry_tokens, entry_cost = tokens, cost
        models.append({"model": entry["model"], "tokens": entry_tokens, "cost_usd": entry_cost})
    row: dict[str, Any] = {
        "schema_version": USAGE_LEDGER_ENTRY_SCHEMA,
        "entry_id": f"{key}#{attempt}",
        "turn_key": key,
        "host_attempt": attempt,
        "goal_id": goal_id,
        "agent_id": _token(agent_id),
        "role": _token(role),
        "todo_id": _token(todo_id),
        "provider": _token(provider),
        "model": primary,
        "models": models,
        "host": usage["host"],
        "usage_source": usage["source"],
        "outcome": _token(outcome),
        "status": _token(status),
        "failure_kind": _token(failure_kind),
        "started_at": usage["started_at"],
        "finished_at": usage["finished_at"],
        "duration_ms": usage["duration_ms"],
        "num_turns": usage.get("num_turns"),
        "tokens": tokens,
        "cost_usd": cost,
        "cost_estimated": cost_source == COST_SOURCE_ESTIMATED,
        "cost_source": cost_source,
        "recorded_at": _now_iso(),
    }
    for field in ("host_duration_ms", "api_duration_ms", "session_ref"):
        if field in usage:
            row[field] = usage[field]
    row.update(extra)
    return row


def record_turn_usage(
    runtime_root: Path,
    goal_id: str,
    turn_usage: Mapping[str, Any],
    **lineage: Any,
) -> dict[str, Any]:
    """Append one row unless the same Turn attempt is already recorded."""

    path = usage_ledger_path(runtime_root, goal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(path):
        rows = read_usage_entries(runtime_root, goal_id)
        row = build_usage_entry(turn_usage, goal_id=goal_id, previous_rows=rows, **lineage)
        if any(existing.get("entry_id") == row["entry_id"] for existing in rows):
            return {"recorded": False, "duplicate": True, "entry_id": row["entry_id"]}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
            handle.flush()
    return {
        "recorded": True,
        "entry_id": row["entry_id"],
        "cost_usd": row["cost_usd"],
        "cost_source": row["cost_source"],
    }


def goal_spend_usd(runtime_root: Path, goal_id: str) -> float:
    """Total recorded cost of one goal (reported plus estimated)."""

    return round(
        sum(
            float(row["cost_usd"])
            for row in read_usage_entries(runtime_root, goal_id)
            if isinstance(row.get("cost_usd"), (int, float)) and not isinstance(row.get("cost_usd"), bool)
        ),
        8,
    )
