"""Optional per-goal spend budget (fork gap G9).

``goals/<goal>/usage-budget.json`` holds ``budget_usd``; ``loopx usage budget``
sets or clears it. The dispatcher compares the ledger's total cost with it:
at 80% it opens one non-blocking ``user_action`` alert, at 100% one
``budget_exhausted`` user gate that pauses the goal's new Turns until the
owner decides (``loopx.usage_budget_gate``, design decision 41).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..history import validate_goal_id_path_segment
from ..registry import atomic_write_json
from .ledger import goal_spend_usd

USAGE_BUDGET_SCHEMA_VERSION = "loopx_usage_budget_v0"
USAGE_BUDGET_THRESHOLDS = (0.8, 1.0)
_MAX_BUDGET_USD = 10**7


def usage_budget_path(runtime_root: Path, goal_id: str) -> Path:
    return (
        Path(runtime_root).expanduser()
        / "goals"
        / validate_goal_id_path_segment(goal_id)
        / "usage-budget.json"
    )


def read_usage_budget_record(runtime_root: Path, goal_id: str) -> dict[str, Any] | None:
    """The valid budget file (``budget_usd`` and ``updated_at``), else None."""

    try:
        payload = json.loads(usage_budget_path(runtime_root, goal_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = payload.get("budget_usd") if isinstance(payload, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not (math.isfinite(value) and value > 0):
        return None
    return {"budget_usd": float(value), "updated_at": str(payload.get("updated_at") or "")}


def read_usage_budget(runtime_root: Path, goal_id: str) -> float | None:
    record = read_usage_budget_record(runtime_root, goal_id)
    return record["budget_usd"] if record else None


def write_usage_budget(runtime_root: Path, goal_id: str, budget_usd: float | None) -> dict[str, Any]:
    path = usage_budget_path(runtime_root, goal_id)
    if budget_usd is None:
        path.unlink(missing_ok=True)
        return {"goal_id": goal_id, "budget_usd": None}
    if not math.isfinite(budget_usd) or budget_usd <= 0 or budget_usd > _MAX_BUDGET_USD:
        raise ValueError(f"budget must be a positive USD amount up to {_MAX_BUDGET_USD}")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        path,
        {
            "schema_version": USAGE_BUDGET_SCHEMA_VERSION,
            "goal_id": goal_id,
            "budget_usd": round(float(budget_usd), 2),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    return {"goal_id": goal_id, "budget_usd": round(float(budget_usd), 2)}


def budget_status(
    runtime_root: Path, goal_id: str, *, spent_usd: float | None = None
) -> dict[str, Any] | None:
    """``None`` without a budget; otherwise spend, ratio and crossed thresholds."""

    budget = read_usage_budget(runtime_root, goal_id)
    if budget is None:
        return None
    spent = goal_spend_usd(runtime_root, goal_id) if spent_usd is None else spent_usd
    ratio = spent / budget
    return {
        "budget_usd": budget,
        "spent_usd": round(spent, 6),
        "spent_ratio": round(ratio, 4),
        "crossed": [threshold for threshold in USAGE_BUDGET_THRESHOLDS if ratio >= threshold],
    }
