"""Pure scheduling rules for the dispatcher (no I/O).

The dispatcher never decides *what* an agent works on. LoopX's quota
``should-run`` decision does. These rules only answer when a launch is
allowed: slots, cooldowns and how a finished child's outcome is classified.
"""

from __future__ import annotations
from ..control_plane.quota.effective_action import EffectiveAction

import json
from collections.abc import Mapping
from typing import Any

ROLE_ORCHESTRATOR = "orchestrator"
ROLE_DEVELOPER = "developer"
ROLE_ACCEPTOR = "acceptor"

# BuiltInHostError failure kinds (claude_code.py / codex_cli.py) that mean the
# provider, not the agent or the task, refused work for now.
PROVIDER_BACKOFF_FAILURE_KINDS = frozenset(
    {"rate_limited", "quota_exhausted", "provider_capacity"}
)
AUTH_FAILURE_KINDS = frozenset({"auth_failed"})

OUTCOME_COMMITTED = "committed"
OUTCOME_FAILED = "failed"
OUTCOME_HOST_FAILED = "host_failed"
OUTCOME_CRASHED = "crashed"


def slot_limit(role: str | None, max_concurrency: int) -> int:
    """Orchestrators run strictly serially (decision 19); others use their cap."""

    if role == ROLE_ORCHESTRATOR:
        return 1
    return max(1, int(max_concurrency or 1))


def backoff_seconds(failures: int, *, base: float, cap: float) -> float:
    """Exponential backoff: base, 2*base, 4*base ... capped."""

    exponent = max(0, int(failures) - 1)
    return float(min(cap, base * (2 ** min(exponent, 30))))


def decide_turn(
    payload: Mapping[str, Any] | None,
    *,
    role: str | None,
    state_changed: bool,
) -> dict[str, Any]:
    """Turn a quota should-run payload into a launch decision.

    - ``should_run`` false: never launch.
    - A selected Todo in this agent's lane: launch for it.
    - Without a selected Todo nobody launches. ``turn run-once`` refuses every
      host route that lacks todo lineage ("host-bound routes require goal,
      agent, todo, and action-hash lineage"), so a todo-less orchestrator
      Turn could only fail without calling the host, and the E2E pilot's
      resident dispatcher relaunched it every few seconds. The orchestrator
      is event-triggered through its todos instead: intake planning,
      escalations, and gate threads awaiting it (which unblock its lane).
      A pending orchestrator action is reported, not launched.
    """

    payload = payload if isinstance(payload, Mapping) else {}
    if payload.get("should_run") is not True:
        return {"launch": False, "reason": "should_run_false", "detail": str(payload.get("reason") or "")[:200]}
    selected = payload.get("selected_todo")
    todo_id = selected.get("todo_id") if isinstance(selected, Mapping) else None
    todo_role = selected.get("role") if isinstance(selected, Mapping) else None
    if todo_id:
        return {
            "launch": True,
            "reason": "selected_todo",
            "todo_id": str(todo_id),
            "todo_is_agent_todo": todo_role in {None, "agent"},
        }
    if role in {ROLE_DEVELOPER, ROLE_ACCEPTOR}:
        return {"launch": False, "reason": "no_selected_todo"}
    effective_action = str(payload.get("effective_action") or "")
    if effective_action and effective_action != EffectiveAction.NORMAL_RUN.value:
        return {"launch": False, "reason": "orchestrator_action_without_todo",
                "detail": f"effective_action={effective_action}"}
    return {"launch": False, "reason": "orchestrator_idle"}


def alternate_todo(
    payload: Mapping[str, Any] | None, *, exclude: set[str] | frozenset[str],
) -> str | None:
    """Another executable todo from the same should-run lane, for a free slot.

    ``should-run`` always selects the lane's first executable todo, so a
    second slot of the same agent would get the in-flight todo again. The
    lane's ``first_executable_items`` is already role- and claim-filtered by
    LoopX; the Turn is pinned with ``--todo-id`` and run-once re-checks it.
    """

    summary = (payload or {}).get("agent_todo_summary") if isinstance(payload, Mapping) else None
    items = summary.get("first_executable_items") if isinstance(summary, Mapping) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        todo_id = str(item.get("todo_id") or "")
        if not todo_id or todo_id in exclude:
            continue
        if str(item.get("status") or "open") not in {"open", "in_review"}:
            continue
        if str(item.get("role") or "agent") != "agent":
            continue
        return todo_id
    return None


def classify_outcome(returncode: int | None, stdout_text: str) -> dict[str, Any]:
    """Classify one finished ``turn run-once`` child from its exit and JSON output."""

    payload: Any = None
    text = (stdout_text or "").strip()
    if text:
        try:
            payload = json.loads(text)
        except ValueError:
            start = text.find("{")
            if start >= 0:
                try:
                    payload = json.loads(text[start:])
                except ValueError:
                    payload = None
    if not isinstance(payload, Mapping):
        return {
            "outcome": OUTCOME_CRASHED,
            "returncode": returncode,
            "failure_kind": None,
            "status": None,
        }
    host_failure = payload.get("host_failure")
    failure_kind = (
        str(host_failure.get("kind") or "") or None
        if isinstance(host_failure, Mapping)
        else None
    )
    status = payload.get("status")
    if payload.get("ok") is True and returncode in {0, None}:
        outcome = OUTCOME_COMMITTED
    elif failure_kind:
        outcome = OUTCOME_HOST_FAILED
    else:
        outcome = OUTCOME_FAILED
    error = payload.get("error")
    return {
        "outcome": outcome,
        "returncode": returncode,
        "failure_kind": failure_kind,
        "status": str(status) if status is not None else None,
        "error": str(error)[:300] if error else None,
    }
