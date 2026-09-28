"""Keep a Turn's identity when its settlement fails after the host completed.

Quota spend and refresh-state retry run-index and state-file races a bounded
number of times (design-v0 decision 32). A race that outlasts them ends the
``turn run-once`` child with a failed settlement: either an error payload
(the retry raised) or a journal ``status=failed`` whose receipt names a
settlement phase. In both cases the Turn journal already holds the host's
typed result.

Minting a new Turn for the next launch would lose that Turn's quota spend or
make the host redo its work. The dispatcher instead records a ``retry_turns``
entry, like the crash branch, and relaunches the same Turn identity with
``--resume-turn-key``; run-once then skips the host because ``typed_result``
is complete and continues from the last side-effect-safe phase.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..control_plane.turn_driver.journal_store import load_turn_journal, turn_journal_path

# Phases that settle a completed host result; a failure here keeps the Turn.
SETTLEMENT_FAILED_PHASES = frozenset(
    {"durable_writeback", "quota_spend", "terminal_closeout", "scheduler_apply", "scheduler_ack"}
)
# A settlement that still fails after this many resumes falls back to a new Turn.
SETTLEMENT_RETRY_LIMIT = 5
# The should-run payload shape that asks decide_turn to resume one settlement.
SETTLEMENT_RESUME_PAYLOAD_KEY = "settlement_resume"


def _payload(stdout_text: str) -> Mapping[str, Any] | None:
    text = (stdout_text or "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        start = text.find("{")
        if start < 0:
            return None
        try:
            value = json.loads(text[start:])
        except ValueError:
            return None
    return value if isinstance(value, Mapping) else None


def unsettled_turn(runtime_root: Path, goal_id: str, stdout_text: str) -> dict[str, Any] | None:
    """The journaled Turn whose settlement failed after its host completed, if any.

    Returns ``{"turn_key", "failed_phase"}`` when the child's payload names a
    journal whose host result is cached (``typed_result`` completed) and whose
    settlement is unfinished: still ``in_progress`` because a settlement
    effect raised, or ``failed`` in a settlement phase.
    """

    payload = _payload(stdout_text)
    if payload is None or payload.get("ok") is True:
        return None
    turn_key = str(payload.get("resume_turn_key") or "")
    if not turn_key:
        return None
    try:
        journal = load_turn_journal(turn_journal_path(runtime_root, goal_id=goal_id, turn_key=turn_key))
    except (OSError, ValueError):
        return None
    if journal is None or "typed_result" not in list(journal.get("completed_phases") or []):
        return None
    status = journal.get("status")
    receipt = journal.get("receipt") if isinstance(journal.get("receipt"), Mapping) else {}
    if status == "in_progress":
        attempts = journal.get("effect_attempts") if isinstance(journal.get("effect_attempts"), Mapping) else {}
        failed_phase = next(iter(attempts), None) or "settlement"
    elif status == "failed" and receipt.get("failed_phase") in SETTLEMENT_FAILED_PHASES:
        failed_phase = str(receipt["failed_phase"])
    else:
        return None
    return {"turn_key": turn_key, "failed_phase": str(failed_phase)}


def resume_payload(todo_id: str) -> dict[str, Any]:
    """A should-run stand-in that makes the dispatcher resume one settlement."""

    return {SETTLEMENT_RESUME_PAYLOAD_KEY: True, "should_run": True, "todo_id": todo_id}


def pending_resume_todo(
    retries: Mapping[str, Any],
    *,
    goal_id: str,
    agent_id: str,
    excluded: set[str],
) -> str | None:
    """A todo of this agent whose failed settlement waits for a resume."""

    prefix = f"{goal_id}/"
    suffix = f"@{agent_id}"
    for key, retry in retries.items():
        if not (isinstance(retry, Mapping) and retry.get("settlement") and key.startswith(prefix)):
            continue
        if not key.endswith(suffix):
            continue
        todo_id = str(retry.get("todo_id") or "")
        if todo_id and todo_id not in excluded:
            return todo_id
    return None
