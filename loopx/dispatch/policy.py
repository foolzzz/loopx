"""Pure scheduling rules for the dispatcher (no I/O).

The dispatcher never decides *what* an agent works on. LoopX's quota
``should-run`` decision does. These rules only answer when a launch is
allowed: slots, cooldowns and how a finished child's outcome is classified.
"""

from __future__ import annotations
from ..control_plane.quota.effective_action import EffectiveAction
from ..control_plane.turn_driver.host_stderr import redact_host_stderr_line

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

ROLE_ORCHESTRATOR = "orchestrator"
ROLE_DEVELOPER = "developer"
ROLE_ACCEPTOR = "acceptor"
# A pinned todo-lane Turn on a deferred todo is always refused by run-once
# (the requested todo is not eligible), so the pass never launches one.
SELECTED_TODO_DEFERRED_REASON = "selected_todo_deferred"

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
# A failed Turn's error in the pass log and dispatch status: one redacted line.
FAILURE_TEXT_MAX_CHARS = 300
# Absolute paths outside the known roots (see failure_text). A match never
# spans whitespace, except a quoted path, whose quotes delimit it. The
# lookbehinds leave URLs, relative paths, ratios and paths after a placeholder.
_PATH_SEGMENT = r"[^\s/\\\"'`<>|,;:()\[\]{}]+"
_ABSOLUTE_PATH = re.compile(
    r"(?P<quote>[\"'`])(?:~?/|[A-Za-z]:[/\\]|\\\\)[^\"'`\n]*(?P=quote)"
    r"|\bfile://[^\s\"'`<>]*"
    rf"|(?<![\w.:/\\~>-])(?:~(?:/{_PATH_SEGMENT})+|/{_PATH_SEGMENT}(?:/{_PATH_SEGMENT})+)/?"
    rf"|(?<![\w\\])(?:[A-Za-z]:|\\\\{_PATH_SEGMENT})(?:[/\\]{_PATH_SEGMENT})+[/\\]?"
)


def slot_limit(role: str | None, max_concurrency: int) -> int:
    """Orchestrators run strictly serially (decision 19); others use their cap."""

    if role == ROLE_ORCHESTRATOR:
        return 1
    return max(1, int(max_concurrency or 1))


def goal_is_role_v1(goal: Mapping[str, Any] | None) -> bool:
    """Whether the goal runs the role_v1 agent model (the default)."""

    from ..todo_acceptance import goal_uses_role_v1

    return goal_uses_role_v1(goal)


def todo_scoped_lane(*, role_v1: bool, registry_role: str | None) -> bool:
    """Whether this agent's Turns run in per-todo lanes (design-v0 decision 32).

    Mirrors run-once's lane choice (``lane_fence.turn_lane_todo_scope``): under
    role_v1 a registered developer or acceptor is fenced per (goal, todo), so it
    may run up to ``max_concurrency`` todos of one goal at once. The
    orchestrator stays serial per goal, and peer_v1 keeps one lane per agent.
    """

    from ..control_plane.turn_driver.lane_fence import TURN_LANE_TODO_SCOPED_ROLES

    return role_v1 and registry_role in TURN_LANE_TODO_SCOPED_ROLES


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
      A pending orchestrator action is reported, not launched; the
      dispatcher then opens one orchestrator todo for it
      (``orchestrator_actions``), so the next pass has a todo to launch.
    """

    payload = payload if isinstance(payload, Mapping) else {}
    if payload.get("settlement_resume") is True and payload.get("todo_id"):
        # A Turn whose settlement failed after its host completed resumes
        # under its own identity (dispatch.settlement_retry); run-once skips
        # the host, so no workspace or todo selection applies.
        return {
            "launch": True,
            "reason": "settlement_retry",
            "todo_id": str(payload["todo_id"]),
            "todo_is_agent_todo": False,
        }
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
            "todo_status": str(selected.get("status") or ""),
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


def classify_outcome(
    returncode: int | None, stdout_text: str, *, roots: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Classify one finished ``turn run-once`` child from its exit and JSON output.

    ``roots`` are the known local roots for ``failure_text``.
    """

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
    if not error and outcome != OUTCOME_COMMITTED:
        # A journaled failure (validation, settlement) names its reason instead.
        error = payload.get("reason")
    error_code = payload.get("error_code")
    return {
        "outcome": outcome,
        "returncode": returncode,
        "failure_kind": failure_kind,
        "status": str(status) if status is not None else None,
        "error": failure_text(error, roots=roots),
        "error_code": error_code[:80] if isinstance(error_code, str) and error_code else None,
    }


def failure_text(value: Any, *, roots: Iterable[tuple[str, str]] = ()) -> str | None:
    """A failed Turn's error as one line, with local paths and credentials redacted, then bounded.

    ``roots`` pairs each known local root (the dispatcher passes its runtime
    root, state home, project and home, as given and resolved) with a
    placeholder such as ``<runtime-root>``. They are replaced exactly, longest
    first and only at path boundaries, so a root that holds spaces is masked
    whole. Any other absolute path is then masked as ``<path>``. That match
    stops at whitespace, so an unquoted path with spaces outside the known
    roots may be masked only in part. The text stays in the owner's local
    dispatcher log and status.
    """

    text = " ".join(str(value or "").split())
    if not text:
        return None
    for root, label in sorted(roots, key=lambda item: len(item[0]), reverse=True):
        root = " ".join(root.split()).rstrip("/\\")
        if root:  # never the filesystem root alone
            text = re.sub(rf"(?<![\w.~-]){re.escape(root)}(?![\w.-])", lambda _match: label, text)
    text = _ABSOLUTE_PATH.sub(lambda match: f"{match['quote'] or ''}<path>{match['quote'] or ''}", text)
    return redact_host_stderr_line(text)[:FAILURE_TEXT_MAX_CHARS]
