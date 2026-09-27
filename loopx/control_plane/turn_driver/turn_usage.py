"""Typed per-Turn usage observed by a built-in host (fork gap G9).

A built-in host (claude-code, codex-cli) knows what one Turn cost: Claude
Code's headless JSON result reports ``total_cost_usd``, ``num_turns``,
durations, token ``usage`` and ``modelUsage``; ``codex exec --json`` reports
token usage on its ``turn.completed`` event. This module turns those raw host
outputs into one bounded, content-free ``turn_usage`` block. It never copies
prompt, result or diagnostic text, session ids (only a digest) or credentials.

The block travels beside the typed host result, not inside it: the agent's
schema-bound result stays exactly the Turn contract. A host returns a
:class:`HostResultWithUsage` (a plain ``dict`` carrying ``turn_usage`` as an
attribute) on success and sets ``BuiltInHostError.turn_usage`` on failure, so
the executor can journal usage for failed Turns too: the money was spent.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

TURN_USAGE_SCHEMA_VERSION = "loopx_turn_usage_v0"
TURN_USAGE_HOSTS = ("claude-code", "codex-cli")
TURN_USAGE_SOURCES = ("claude_result_envelope", "codex_exec_events", "wall_clock_only")
TURN_USAGE_TOKEN_FIELDS = (
    "input",
    "cached_input",
    "cache_creation_input",
    "output",
    "reasoning_output",
)
MAX_TURN_USAGE_MODELS = 8
MAX_TURN_USAGE_MODEL_CHARS = 120
# A single Turn will not exceed these; anything larger is a parse error.
_MAX_TOKENS = 10**12
_MAX_COST_USD = 10**6
_MAX_DURATION_MS = 7 * 24 * 3600 * 1000
_MAX_NUM_TURNS = 10**6


class HostResultWithUsage(dict):
    """A host result ``dict`` that also carries the Turn's ``turn_usage``."""

    turn_usage: dict[str, Any] | None = None


def attach_turn_usage(result: Mapping[str, Any], usage: Mapping[str, Any] | None) -> dict[str, Any]:
    annotated = HostResultWithUsage(result)
    annotated.turn_usage = dict(usage) if isinstance(usage, Mapping) else None
    return annotated


def host_turn_usage(value: Any) -> dict[str, Any] | None:
    """Return the normalized usage a host attached to a result or an error."""

    usage = getattr(value, "turn_usage", None)
    if not isinstance(usage, Mapping):
        return None
    try:
        return normalize_turn_usage(usage)
    except ValueError:
        return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    number = int(value)
    return number if 0 <= number <= _MAX_TOKENS else None


def _money(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > _MAX_COST_USD:
        return None
    return round(number, 8)


def _model_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    # Model ids may contain "/" (gateway namespaces) but never a local path.
    if not text or text[0] in "/~." or any(ch in text for ch in "\x00\r\n\\"):
        return None
    return text[:MAX_TURN_USAGE_MODEL_CHARS]


def session_ref(session_id: str | None) -> str | None:
    """An opaque digest of a host session id; the id itself is never recorded."""

    if not session_id:
        return None
    return "sha256:" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


def _tokens(values: Mapping[str, Any]) -> dict[str, int]:
    tokens = {field: _count(values.get(field)) or 0 for field in TURN_USAGE_TOKEN_FIELDS}
    tokens["total"] = (
        tokens["input"] + tokens["cached_input"] + tokens["cache_creation_input"] + tokens["output"]
    )
    return tokens


def _base(
    *,
    host: str,
    source: str,
    started_at: float,
    finished_at: float,
) -> dict[str, Any]:
    duration_ms = max(0, min(_MAX_DURATION_MS, int(round((finished_at - started_at) * 1000))))
    return {
        "schema_version": TURN_USAGE_SCHEMA_VERSION,
        "host": host,
        "source": source,
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
        "duration_ms": duration_ms,
        "models": [],
        "tokens": None,
        "cost_usd": None,
        "cost_estimated": False,
        "num_turns": None,
    }


def wall_clock_turn_usage(*, host: str, started_at: float, finished_at: float) -> dict[str, Any]:
    """Usage when the host produced no accounting (timeout, crash): duration only."""

    return _base(host=host, source="wall_clock_only", started_at=started_at, finished_at=finished_at)


def claude_code_turn_usage(
    envelope: Mapping[str, Any] | None,
    *,
    started_at: float,
    finished_at: float,
    configured_model: str | None = None,
) -> dict[str, Any]:
    """Map Claude Code's ``--output-format json`` result object to ``turn_usage``."""

    if not isinstance(envelope, Mapping):
        return wall_clock_turn_usage(host="claude-code", started_at=started_at, finished_at=finished_at)
    usage = _base(
        host="claude-code",
        source="claude_result_envelope",
        started_at=started_at,
        finished_at=finished_at,
    )
    raw = envelope.get("usage") if isinstance(envelope.get("usage"), Mapping) else {}
    details = raw.get("output_tokens_details") if isinstance(raw.get("output_tokens_details"), Mapping) else {}
    if raw:
        usage["tokens"] = _tokens(
            {
                "input": raw.get("input_tokens"),
                "cached_input": raw.get("cache_read_input_tokens"),
                "cache_creation_input": raw.get("cache_creation_input_tokens"),
                "output": raw.get("output_tokens"),
                "reasoning_output": details.get("thinking_tokens"),
            }
        )
    models: list[dict[str, Any]] = []
    model_usage = envelope.get("modelUsage") if isinstance(envelope.get("modelUsage"), Mapping) else {}
    for name, entry in model_usage.items():
        model = _model_name(name)
        if model is None or not isinstance(entry, Mapping):
            continue
        models.append(
            {
                "model": model,
                "tokens": _tokens(
                    {
                        "input": entry.get("inputTokens"),
                        "cached_input": entry.get("cacheReadInputTokens"),
                        "cache_creation_input": entry.get("cacheCreationInputTokens"),
                        "output": entry.get("outputTokens"),
                        "reasoning_output": entry.get("thinkingTokens"),
                    }
                ),
                "cost_usd": _money(entry.get("costUSD")),
            }
        )
    models.sort(key=lambda item: -(item["cost_usd"] or 0.0))
    if not models and (model := _model_name(configured_model)):
        models.append({"model": model, "tokens": usage["tokens"], "cost_usd": None})
    usage["models"] = models[:MAX_TURN_USAGE_MODELS]
    usage["cost_usd"] = _money(envelope.get("total_cost_usd"))
    num_turns = _count(envelope.get("num_turns"))
    usage["num_turns"] = num_turns if num_turns is not None and num_turns <= _MAX_NUM_TURNS else None
    for field, key in (("host_duration_ms", "duration_ms"), ("api_duration_ms", "duration_api_ms")):
        value = _count(envelope.get(key))
        if value is not None and value <= _MAX_DURATION_MS:
            usage[field] = value
    return usage


def codex_event_token_usage(event: Mapping[str, Any]) -> dict[str, int] | None:
    """Token usage from one ``codex exec --json`` event, if it carries any.

    ``turn.completed`` carries ``usage`` (input includes cached input; output
    includes reasoning). The older protocol ``token_count`` event carries
    ``info.total_token_usage`` with the same fields. Both are session totals.
    """

    raw: Any = None
    kind = event.get("type")
    if kind in {"turn.completed", "turn.failed"}:
        raw = event.get("usage")
    else:
        msg = event.get("msg") if isinstance(event.get("msg"), Mapping) else event
        if msg.get("type") == "token_count":
            info = msg.get("info") if isinstance(msg.get("info"), Mapping) else {}
            raw = info.get("total_token_usage")
    if not isinstance(raw, Mapping):
        return None
    total_input = _count(raw.get("input_tokens")) or 0
    cached = min(_count(raw.get("cached_input_tokens")) or 0, total_input)
    return _tokens(
        {
            "input": total_input - cached,
            "cached_input": cached,
            "cache_creation_input": raw.get("cache_write_input_tokens"),
            "output": raw.get("output_tokens"),
            "reasoning_output": raw.get("reasoning_output_tokens"),
        }
    )


def codex_cli_turn_usage(
    session_tokens: dict[str, int] | None,
    *,
    started_at: float,
    finished_at: float,
    model: str | None,
    session_id: str | None,
    resumed: bool,
) -> dict[str, Any]:
    """Map the last observed codex token totals to ``turn_usage``.

    Codex reports totals for the whole session, so a resumed session's numbers
    include earlier Turns. The block marks them ``cumulative`` with the
    session digest; the usage ledger subtracts the previous Turn of the same
    session before it counts or prices them.
    """

    if session_tokens is None:
        usage = wall_clock_turn_usage(host="codex-cli", started_at=started_at, finished_at=finished_at)
    else:
        usage = _base(host="codex-cli", source="codex_exec_events", started_at=started_at, finished_at=finished_at)
        usage["tokens"] = dict(session_tokens)
        usage["num_turns"] = 1
    if model_name := _model_name(model):
        usage["models"] = [{"model": model_name, "tokens": usage["tokens"], "cost_usd": None}]
    if ref := session_ref(session_id):
        usage["session_ref"] = ref
        usage["session_resumed"] = bool(resumed)
        usage["cumulative"] = usage["tokens"] is not None
    return usage


def _normalize_tokens(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("turn_usage tokens must be an object")
    tokens = {}
    for field in TURN_USAGE_TOKEN_FIELDS:
        count = _count(value.get(field, 0))
        if count is None:
            raise ValueError(f"turn_usage tokens.{field} must be a non-negative integer")
        tokens[field] = count
    tokens["total"] = (
        tokens["input"] + tokens["cached_input"] + tokens["cache_creation_input"] + tokens["output"]
    )
    return tokens


def normalize_turn_usage(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one ``turn_usage`` block and drop anything not in the contract."""

    if value.get("schema_version") != TURN_USAGE_SCHEMA_VERSION:
        raise ValueError("unsupported turn_usage schema")
    host = value.get("host")
    source = value.get("source")
    if host not in TURN_USAGE_HOSTS or source not in TURN_USAGE_SOURCES:
        raise ValueError("turn_usage host or source is unsupported")
    duration = _count(value.get("duration_ms"))
    if duration is None or duration > _MAX_DURATION_MS:
        raise ValueError("turn_usage duration_ms is invalid")
    normalized: dict[str, Any] = {
        "schema_version": TURN_USAGE_SCHEMA_VERSION,
        "host": host,
        "source": source,
        "started_at": str(value.get("started_at") or "")[:40],
        "finished_at": str(value.get("finished_at") or "")[:40],
        "duration_ms": duration,
        "tokens": _normalize_tokens(value.get("tokens")),
        "cost_usd": _money(value.get("cost_usd")),
        "cost_estimated": value.get("cost_estimated") is True,
        "num_turns": _count(value.get("num_turns")),
    }
    models = []
    for entry in value.get("models") or []:
        if not isinstance(entry, Mapping) or (model := _model_name(entry.get("model"))) is None:
            continue
        models.append(
            {
                "model": model,
                "tokens": _normalize_tokens(entry.get("tokens")),
                "cost_usd": _money(entry.get("cost_usd")),
            }
        )
    normalized["models"] = models[:MAX_TURN_USAGE_MODELS]
    for field in ("host_duration_ms", "api_duration_ms"):
        count = _count(value.get(field))
        if count is not None and count <= _MAX_DURATION_MS:
            normalized[field] = count
    ref = value.get("session_ref")
    if isinstance(ref, str) and ref.startswith("sha256:") and len(ref) <= 40:
        normalized["session_ref"] = ref
        normalized["session_resumed"] = value.get("session_resumed") is True
        normalized["cumulative"] = value.get("cumulative") is True
    for field in ("host_attempt",):
        count = _count(value.get(field))
        if count is not None:
            normalized[field] = count
    return normalized


def primary_model(usage: Mapping[str, Any]) -> str | None:
    models = usage.get("models")
    if isinstance(models, list) and models and isinstance(models[0], Mapping):
        return _model_name(models[0].get("model"))
    return None


def iter_model_names(usage: Mapping[str, Any]) -> Iterable[str]:
    for entry in usage.get("models") or []:
        if isinstance(entry, Mapping) and (name := _model_name(entry.get("model"))):
            yield name
