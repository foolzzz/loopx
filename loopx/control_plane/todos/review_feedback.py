"""Fit an acceptor's reject feedback into ``review_feedback`` (fork pilot v1 gap N6).

``review_feedback`` is a 600-character contract field (``TODO_REVIEW_FEEDBACK_LIMIT``,
shared with the TypeScript field planner). Raising it would change the
coordination contract, so the verdict text is fitted instead:

- trailing host or terminal glyphs (control, replacement or box-drawing
  characters, and a short run of CJK or fullwidth glyphs after text that has
  none) are stripped;
- when the text is too long, the sentences that name a failed criterion move
  first (each group keeps its order) and the text is cut once, at a word
  boundary, with ``...``. The whole 600 characters are used.
"""

from __future__ import annotations

import re
import unicodedata

from .contract import TODO_REVIEW_FEEDBACK_LIMIT, compact_todo_text

REVIEW_FEEDBACK_CUT_MARKER = "..."
# Sentences that name what failed are kept first when the feedback is cut.
_CRITERIA_SENTENCE = re.compile(r"criteri|\bfail|\bmissing\b|\bnot met\b", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
# A host that hits its output bound can end the text in a short junk run.
_MAX_TRAILING_JUNK = 8


def _junk_glyph(char: str) -> bool:
    category = unicodedata.category(char)
    if category in {"Cc", "Cf", "Co", "Cs", "Cn"} or char == "\ufffd":
        return True
    return 0x2500 <= ord(char) <= 0x259F  # box drawing and block elements


def _wide_glyph(char: str) -> bool:
    """CJK, kana, hangul and fullwidth forms: what a cut-off host emits as junk."""

    code = ord(char)
    return (
        0x2E80 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF
        or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFFEF
    )


def strip_trailing_host_glyphs(text: str) -> str:
    """Drop the stray glyphs a host or terminal leaves at the end of a verdict."""

    text = text.rstrip()
    while text and _junk_glyph(text[-1]):
        text = text[:-1].rstrip()
    # A short run of wide glyphs after text that has none is a cut-off artifact
    # (the pilot's reject ended in "local年／"), not the reviewer's language.
    tail = len(text)
    while tail > 0 and (_wide_glyph(text[tail - 1]) or text[tail - 1].isspace()):
        tail -= 1
    body, junk = text[:tail], text[tail:].strip()
    if junk and len(junk) <= _MAX_TRAILING_JUNK and body and not any(_wide_glyph(char) for char in body):
        text = body.rstrip()
    return text


def _clip(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    room = max(0, budget - len(REVIEW_FEEDBACK_CUT_MARKER))
    cut = text[:room]
    space = cut.rfind(" ")
    if space > room // 2:
        cut = cut[:space]
    return cut.rstrip() + REVIEW_FEEDBACK_CUT_MARKER


def fit_review_feedback(prefix: str, feedback: str, *, limit: int = TODO_REVIEW_FEEDBACK_LIMIT) -> str:
    """``prefix + feedback`` within ``limit``, the sentences naming failed criteria first.

    Feedback that fits is kept verbatim. Longer feedback is reordered so the
    sentences that name a failed criterion come first (each group keeps its
    order), then cut once at a word boundary with ``...``, so the whole limit
    is used and what is lost is detail, not a criterion.
    """

    text = strip_trailing_host_glyphs(compact_todo_text(feedback))
    budget = limit - len(prefix)
    if len(text) <= budget:
        return prefix + text
    sentences = [part for part in _SENTENCE_SPLIT.split(text) if part]
    criteria = [sentence for sentence in sentences if _CRITERIA_SENTENCE.search(sentence)]
    details = [sentence for sentence in sentences if not _CRITERIA_SENTENCE.search(sentence)]
    return prefix + _clip(" ".join([*criteria, *details]), budget)
