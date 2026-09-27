"""Acceptor verdict text bounds for review Turns (fork pilot v1 gap N6).

An acceptor Turn that reviews a delivered (``in_review``) Todo returns its
reject feedback in the host result ``summary``. The ordinary Turn summary is
bounded at 400 characters, below the Todo's 600-character ``review_feedback``
contract, and a host that hits a schema ``maxLength`` cuts the text mid-word or
emits stray glyphs. Review Turns therefore get a larger summary bound; the
reject verdict then fits the text into ``review_feedback`` itself
(``loopx.control_plane.todos.review_feedback``), keeping the parts that name
failed criteria first.

Only the bound changes: the result shape, its fields and the 600-character
``review_feedback`` contract stay as they are.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Enough room that a host never runs into the schema bound while it names every
# failed criterion; the stored review_feedback is fitted to its own contract.
ACCEPTOR_VERDICT_SUMMARY_LIMIT = 2_000


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def turn_reviews_delivered_todo(request_or_plan: Mapping[str, Any] | None) -> bool:
    """Whether the Turn's selected Todo is a delivery under review (``in_review``)."""

    from .driver import selected_turn_todo

    envelope = _mapping(_mapping(request_or_plan).get("turn_envelope"))
    if not envelope:
        return False
    selected = _mapping(_mapping(envelope.get("action")).get("selected_todo"))
    if not selected:
        selected = selected_turn_todo(envelope)
    return str(selected.get("status") or "") == "in_review"


def host_result_text_limits(
    base_limits: tuple[tuple[str, int], ...],
    request_or_plan: Mapping[str, Any] | None,
) -> dict[str, int]:
    """The host result text bounds for this Turn; review Turns get a larger summary."""

    limits = dict(base_limits)
    if turn_reviews_delivered_todo(request_or_plan):
        limits["summary"] = max(limits.get("summary", 0), ACCEPTOR_VERDICT_SUMMARY_LIMIT)
    return limits
