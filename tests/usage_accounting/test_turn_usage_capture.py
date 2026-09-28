"""G9: per-Turn usage capture from real-shaped claude-code and codex-cli output."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from loopx.control_plane.turn_driver.claude_code import run_claude_code_host
from loopx.control_plane.turn_driver.codex_cli import run_codex_cli_host
from loopx.control_plane.turn_driver.host_failure import BuiltInHostError
from loopx.control_plane.turn_driver.turn_usage import (
    TURN_USAGE_SCHEMA_VERSION,
    claude_code_turn_usage,
    codex_cli_turn_usage,
    codex_event_token_usage,
    host_turn_usage,
    normalize_turn_usage,
)
from tests.test_loopx_turn_codex_cli import _request

# Shape captured from `claude -p --output-format json` (Claude Code 2.1.x);
# ids replaced with fixture values.
CLAUDE_RESULT_ENVELOPE = {
    "duration_api_ms": 2991,
    "stop_reason": "end_turn",
    "session_id": "00000000-0000-4000-8000-000000000001",
    "total_cost_usd": 0.04702625,
    "usage": {
        "input_tokens": 10,
        "cache_creation_input_tokens": 37165,
        "cache_read_input_tokens": 0,
        "output_tokens": 112,
        "output_tokens_details": {"thinking_tokens": 106},
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": "standard",
        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 37165},
        "inference_geo": "not_available",
        "iterations": [],
        "speed": "standard",
    },
    "modelUsage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 10,
            "outputTokens": 112,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 37165,
            "webSearchRequests": 0,
            "costUSD": 0.04702625,
            "contextWindow": 200000,
            "maxOutputTokens": 32000,
            "thinkingTokens": 106,
            "canonicalModel": "claude-haiku-4-5",
            "provider": "firstParty",
            "costBasis": "list",
        }
    },
    "permission_denials": [],
    "terminal_reason": "completed",
    "is_error": False,
    "num_turns": 1,
    "subtype": "success",
    "api_error_status": None,
    "result": "ok",
    "type": "result",
    "duration_ms": 3663,
    "uuid": "00000000-0000-4000-8000-000000000002",
}

# Lines captured from `codex exec --json` (codex-cli 0.153): a fresh Turn, then
# the same thread resumed. turn.completed usage is the session total.
CODEX_FRESH_EVENTS = [
    {"type": "thread.started", "thread_id": "01a0e042-0000-7000-8000-000000000001"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "ok"}},
    {"type": "turn.completed", "usage": {"input_tokens": 17977, "cached_input_tokens": 0,
                                         "cache_write_input_tokens": 0, "output_tokens": 5,
                                         "reasoning_output_tokens": 0}},
]
CODEX_RESUMED_COMPLETED = {
    "type": "turn.completed",
    "usage": {"input_tokens": 35971, "cached_input_tokens": 17792, "cache_write_input_tokens": 0,
              "output_tokens": 10, "reasoning_output_tokens": 0},
}


def test_claude_envelope_maps_cost_turns_durations_tokens_and_models() -> None:
    usage = claude_code_turn_usage(CLAUDE_RESULT_ENVELOPE, started_at=100.0, finished_at=104.5)

    assert usage["schema_version"] == TURN_USAGE_SCHEMA_VERSION
    assert usage["host"] == "claude-code" and usage["source"] == "claude_result_envelope"
    assert usage["cost_usd"] == pytest.approx(0.04702625)
    assert usage["cost_estimated"] is False
    assert usage["num_turns"] == 1
    assert usage["duration_ms"] == 4500
    assert usage["host_duration_ms"] == 3663 and usage["api_duration_ms"] == 2991
    assert usage["tokens"] == {
        "input": 10, "cached_input": 0, "cache_creation_input": 37165,
        "output": 112, "reasoning_output": 106, "total": 37287,
    }
    assert [model["model"] for model in usage["models"]] == ["claude-haiku-4-5-20251001"]
    assert usage["models"][0]["cost_usd"] == pytest.approx(0.04702625)
    # Nothing from the conversation or the session survives.
    text = json.dumps(usage)
    assert "00000000-0000-4000-8000" not in text and '"ok"' not in text
    assert normalize_turn_usage(usage) == usage


def test_claude_without_envelope_records_wall_clock_only() -> None:
    usage = claude_code_turn_usage(None, started_at=10.0, finished_at=12.0, configured_model="opus")
    assert usage["source"] == "wall_clock_only"
    assert usage["duration_ms"] == 2000
    assert usage["tokens"] is None and usage["cost_usd"] is None


def test_codex_events_map_cached_input_and_resumed_session_totals() -> None:
    fresh = codex_event_token_usage(CODEX_FRESH_EVENTS[-1])
    assert fresh == {"input": 17977, "cached_input": 0, "cache_creation_input": 0,
                     "output": 5, "reasoning_output": 0, "total": 17982}
    resumed = codex_event_token_usage(CODEX_RESUMED_COMPLETED)
    assert resumed["input"] == 35971 - 17792 and resumed["cached_input"] == 17792
    assert codex_event_token_usage(CODEX_FRESH_EVENTS[0]) is None
    legacy = codex_event_token_usage({"msg": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 40,
                              "output_tokens": 9, "reasoning_output_tokens": 3}}}})
    assert legacy["input"] == 60 and legacy["cached_input"] == 40 and legacy["reasoning_output"] == 3

    usage = codex_cli_turn_usage(resumed, started_at=0.0, finished_at=1.0, model="gpt-5.6-sol",
                                 session_id="thread-1", resumed=True)
    assert usage["cumulative"] is True and usage["session_resumed"] is True
    assert usage["session_ref"].startswith("sha256:") and "thread-1" not in json.dumps(usage)
    assert usage["cost_usd"] is None and usage["models"][0]["model"] == "gpt-5.6-sol"


def test_normalize_rejects_foreign_shapes_and_local_path_models() -> None:
    with pytest.raises(ValueError):
        normalize_turn_usage({"schema_version": "other"})
    usage = claude_code_turn_usage(None, started_at=0.0, finished_at=1.0, configured_model="/Users/x/model")
    assert usage["models"] == []


FAKE_CLAUDE_USAGE = """
import json, os, re, sys
prompt = sys.stdin.read()
turn_key = re.search(r'"turn_key":"([^"]+)"', prompt).group(1)
envelope = json.loads(os.environ["FAKE_CLAUDE_ENVELOPE"])
result = {
    "schema_version": "loopx_turn_result_v0", "turn_key": turn_key,
    "result_kind": "validated_progress", "completed_phases": ["host_execute", "typed_result"],
    "classification": "fixture_progress", "recommended_action": "Continue the public fixture",
    "next_action": "Run the next public fixture check", "delivery_batch_scale": "implementation",
    "delivery_outcome": "outcome_progress",
    "vision_unchanged_reason": "The fixture objective remains unchanged.",
    "path_delta_mode": "unchanged", "agent_vision_json": "",
    "summary": "One public fixture advanced.", "reward_memory_reflection_json": "",
}
envelope["structured_output"] = result
if os.environ.get("FAKE_CLAUDE_FAIL") == "1":
    envelope.update(is_error=True, subtype="error_during_execution", api_error_status=429,
                    structured_output=None, result="API Error: 429")
    print(json.dumps(envelope)); raise SystemExit(1)
print(json.dumps(envelope))
"""


def _fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    executable = tmp_path / "bin" / name
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def test_claude_host_attaches_usage_to_result_and_to_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_bin(tmp_path, "claude", FAKE_CLAUDE_USAGE)
    monkeypatch.setenv("FAKE_CLAUDE_ENVELOPE", json.dumps(CLAUDE_RESULT_ENVELOPE))
    project = tmp_path / "project"
    project.mkdir()

    result = run_claude_code_host(_request(), project=project, claude_bin=str(executable), timeout_seconds=20)
    usage = host_turn_usage(result)
    assert result["result_kind"] == "validated_progress"
    assert usage is not None and usage["cost_usd"] == pytest.approx(0.04702625)
    assert "turn_usage" not in result  # the typed result itself is unchanged

    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    with pytest.raises(BuiltInHostError) as exc_info:
        run_claude_code_host(_request(), project=project, claude_bin=str(executable), timeout_seconds=20)
    assert exc_info.value.failure_kind == "rate_limited"
    failed = host_turn_usage(exc_info.value)
    assert failed is not None and failed["cost_usd"] == pytest.approx(0.04702625)


FAKE_CODEX_USAGE = """
import json, os, sys
args = sys.argv[1:]
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thread-fixture-0001"}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": json.loads(os.environ["FAKE_CODEX_USAGE"])}), flush=True)
if os.environ.get("FAKE_CODEX_FAIL") == "1":
    raise SystemExit(1)
output = args[args.index("--output-last-message") + 1]
with open(output, "w", encoding="utf-8") as handle:
    json.dump({"result_kind": "validated_progress"}, handle)
"""


def test_codex_host_attaches_session_usage_and_keeps_it_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_bin(tmp_path, "codex", FAKE_CODEX_USAGE)
    monkeypatch.setenv("FAKE_CODEX_USAGE", json.dumps(CODEX_FRESH_EVENTS[-1]["usage"]))
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"

    result = run_codex_cli_host(
        _request(), runtime_root=runtime, project=project, codex_bin=str(executable),
        config_overrides=['model="gpt-5.6-sol"'], timeout_seconds=20,
    )
    usage = host_turn_usage(result)
    assert usage is not None
    assert usage["source"] == "codex_exec_events"
    assert usage["tokens"]["input"] == 17977 and usage["tokens"]["output"] == 5
    assert usage["models"][0]["model"] == "gpt-5.6-sol"
    assert usage["session_resumed"] is False and usage["cumulative"] is True

    monkeypatch.setenv("FAKE_CODEX_FAIL", "1")
    with pytest.raises(BuiltInHostError) as exc_info:
        run_codex_cli_host(
            _request(session_action="resume"), runtime_root=runtime, project=project,
            codex_bin=str(executable), timeout_seconds=20,
        )
    failed = host_turn_usage(exc_info.value)
    assert failed is not None and failed["tokens"]["output"] == 5
    assert failed["session_resumed"] is True
