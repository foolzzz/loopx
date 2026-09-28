"""Native Claude Code host for one governed LoopX Turn.

Launches ``claude -p --output-format json --json-schema <schema>`` with the
Turn request on stdin and the workspace as cwd, then returns the schema-bound
``structured_output`` as the typed host result. The executor still owns the
authoritative result validation, independent task validation, writeback and
quota; this adapter only classifies launch failures and rejects a result that
does not echo the Turn contract.

Sessions are not resumed: every Turn starts a fresh Claude Code session with
``--no-session-persistence`` so no transcript is left behind for LoopX to own.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .codex_cli import (
    OUTPUT_DRAIN_TIMEOUT_SECONDS,
    _has_subagent_topology,
    _prompt,
    _terminate_process,
    codex_cli_result_schema,
)
from .executor import LOOPX_TURN_HOST_REQUEST_SCHEMA_VERSION
from .host_failure import BuiltInHostError
from .transaction import LOOPX_TURN_RESULT_SCHEMA_VERSION, TRANSACTION_PHASES
from .turn_usage import attach_turn_usage, claude_code_turn_usage

CLAUDE_CODE_PERMISSION_MODES = (
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "manual",
    "dontAsk",
    "plan",
)
CLAUDE_CODE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_CLAUDE_CODE_PERMISSION_MODE = "dontAsk"
_STDOUT_MAX_BYTES = 4_000_000
# Flags this adapter owns. Letting extra args re-set them would break the
# typed-result contract or the stdin prompt channel.
_RESERVED_FLAGS = frozenset(
    {
        "-p",
        "--print",
        "--output-format",
        "--json-schema",
        "--input-format",
        "--resume",
        "-r",
        "--continue",
        "-c",
        "--session-id",
    }
)
_HTTP_STATUS_KINDS = {
    401: "auth_failed",
    403: "auth_failed",
    429: "rate_limited",
    500: "provider_overloaded",
    502: "provider_overloaded",
    503: "provider_overloaded",
    529: "provider_overloaded",
}
_DIAGNOSTIC_KINDS = (
    (("not logged in", "please run /login", "invalid api key", "authentication_error",
      "oauth token has expired", "invalid bearer token"), "auth_failed"),
    (("credit balance is too low", "usage limit", "quota exceeded",
      "insufficient_quota"), "quota_exhausted"),
    (("rate limit", "rate_limit", "too many requests"), "rate_limited"),
    (("overloaded", "overloaded_error"), "provider_overloaded"),
)


def claude_code_result_schema(request: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The Turn result schema, with child receipts attributed to claude-code."""

    schema = codex_cli_result_schema(request)
    receipts = schema["properties"].get("child_execution_receipts")
    if isinstance(receipts, dict):
        receipts["items"]["properties"]["runtime_id"] = {
            "type": "string",
            "enum": ["claude-code"],
        }
    return schema


def claude_code_command(
    *,
    claude_bin: str,
    schema: Mapping[str, Any],
    model: str | None,
    permission_mode: str,
    reasoning_effort: str | None,
    system_prompt_file: Path | None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    command = [
        claude_bin,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "--permission-mode",
        permission_mode,
        "--no-session-persistence",
    ]
    if model:
        command.extend(["--model", model])
    if reasoning_effort:
        command.extend(["--effort", reasoning_effort])
    if system_prompt_file is not None:
        command.extend(["--append-system-prompt-file", str(system_prompt_file)])
    command.extend(extra_args)
    return command


def _validate_extra_args(extra_args: Sequence[str]) -> list[str]:
    values = [str(item) for item in extra_args]
    for item in values:
        if not item or "\x00" in item:
            raise ValueError("Claude Code extra args must be non-empty strings")
        if item.split("=", 1)[0] in _RESERVED_FLAGS:
            raise ValueError(f"Claude Code extra arg {item.split('=', 1)[0]} is reserved by LoopX")
    return values


def _diagnostic_kind(text: str) -> str | None:
    lowered = text.lower()
    for markers, kind in _DIAGNOSTIC_KINDS:
        if any(marker in lowered for marker in markers):
            return kind
    return None


def _parse_envelope(stdout: str) -> dict[str, Any] | None:
    """Return the final ``type=result`` object from Claude's JSON output."""

    text = stdout.strip()
    if not text:
        return None
    candidates: list[Any] = []
    try:
        candidates.append(json.loads(text))
    except json.JSONDecodeError:
        for line in text.splitlines():
            try:
                candidates.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    flattened: list[Any] = []
    for item in candidates:
        flattened.extend(item if isinstance(item, list) else [item])
    results = [
        item
        for item in flattened
        if isinstance(item, dict) and item.get("type") == "result"
    ]
    return results[-1] if results else None


def _failure_kind(envelope: Mapping[str, Any] | None, stderr: str) -> str:
    if envelope is not None:
        status = envelope.get("api_error_status")
        if isinstance(status, int) and not isinstance(status, bool):
            if status in _HTTP_STATUS_KINDS:
                return _HTTP_STATUS_KINDS[status]
        subtype = str(envelope.get("subtype") or "")
        if subtype == "error_max_structured_output_retries":
            return "contract_rejected"
        result_text = envelope.get("result")
        if isinstance(result_text, str) and (kind := _diagnostic_kind(result_text)):
            return kind
    return _diagnostic_kind(stderr) or "unknown"


def _structured_result(envelope: Mapping[str, Any]) -> Any:
    structured = envelope.get("structured_output")
    if structured is not None:
        return structured
    result_text = envelope.get("result")
    if isinstance(result_text, str) and result_text.strip():
        try:
            return json.loads(result_text)
        except json.JSONDecodeError:
            return None
    return None


def run_claude_code_host(
    request: Mapping[str, Any],
    *,
    project: Path,
    claude_bin: str = "claude",
    model: str | None = None,
    permission_mode: str = DEFAULT_CLAUDE_CODE_PERMISSION_MODE,
    reasoning_effort: str | None = None,
    system_prompt_file: Path | str | None = None,
    extra_args: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    timeout_seconds: float = 115.0,
) -> dict[str, Any]:
    """Run one Turn on Claude Code and return its typed result object."""

    if request.get("schema_version") != LOOPX_TURN_HOST_REQUEST_SCHEMA_VERSION:
        raise ValueError("unsupported LoopX Turn host request schema")
    if permission_mode not in CLAUDE_CODE_PERMISSION_MODES:
        raise ValueError(
            f"Claude Code permission mode must be one of {CLAUDE_CODE_PERMISSION_MODES}"
        )
    if reasoning_effort is not None and reasoning_effort not in CLAUDE_CODE_EFFORTS:
        raise ValueError(f"Claude Code effort must be one of {CLAUDE_CODE_EFFORTS}")
    session = request.get("session") if isinstance(request.get("session"), Mapping) else {}
    if str(session.get("action") or "") != "start_new":
        raise ValueError("claude-code host only starts fresh sessions; resume is unsupported")
    if _has_subagent_topology(request):
        raise ValueError("claude-code host does not support subagent execution topology yet")
    prompt_path: Path | None = None
    if system_prompt_file is not None:
        prompt_path = Path(system_prompt_file).expanduser()
        if not prompt_path.is_file():
            raise ValueError("Claude Code system prompt file does not exist")
    extra = _validate_extra_args(extra_args)
    child_env = {**os.environ, **dict(env or {})}
    resolved = (
        shutil.which(claude_bin, path=child_env.get("PATH"))
        if os.path.sep not in claude_bin
        else claude_bin
    )
    if not resolved or not Path(resolved).exists():
        raise ValueError("Claude Code executable is unavailable")
    turn_key = str(request.get("turn_key") or "")

    command = claude_code_command(
        claude_bin=str(resolved),
        schema=claude_code_result_schema(request),
        model=model,
        permission_mode=permission_mode,
        reasoning_effort=reasoning_effort,
        system_prompt_file=prompt_path,
        extra_args=extra,
    )
    started_at = time.time()
    proc = subprocess.Popen(
        command,
        cwd=project,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        start_new_session=True,
    )
    stdout_chunks: list[str] = []
    stderr_tail: list[str] = []
    stdout_size = [0]

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_size[0] += len(line)
            if stdout_size[0] <= _STDOUT_MAX_BYTES:
                stdout_chunks.append(line)

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            # Keep a short tail only for classification; it is never returned.
            stderr_tail.append(line[:2000])
            del stderr_tail[:-20]

    readers = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=read_stderr, daemon=True),
    ]
    for reader in readers:
        reader.start()
    assert proc.stdin is not None
    timed_out = False
    try:
        try:
            proc.stdin.write(_prompt(request))
            proc.stdin.close()
        except BrokenPipeError:
            pass
        returncode = proc.wait(timeout=max(1.0, timeout_seconds))
    except subprocess.TimeoutExpired:
        _terminate_process(proc)
        timed_out = True
        returncode = proc.returncode
    except BaseException:
        _terminate_process(proc)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=OUTPUT_DRAIN_TIMEOUT_SECONDS)
    finished_at = time.time()
    envelope = (
        None
        if timed_out or stdout_size[0] > _STDOUT_MAX_BYTES
        else _parse_envelope("".join(stdout_chunks))
    )
    # G9: usage is recorded for failed Turns too; the tokens were still spent.
    usage = claude_code_turn_usage(
        envelope, started_at=started_at, finished_at=finished_at, configured_model=model,
    )
    if timed_out:
        raise BuiltInHostError(
            "claude_code_timeout", failure_kind="executor_timeout", turn_usage=usage,
        )
    if stdout_size[0] > _STDOUT_MAX_BYTES:
        raise BuiltInHostError(
            "claude_code_output_too_large", failure_kind="unknown", turn_usage=usage,
        )
    stderr_text = "".join(stderr_tail)
    if returncode != 0 or envelope is None or envelope.get("is_error") is True:
        kind = _failure_kind(envelope, stderr_text)
        if returncode == 0 and envelope is None:
            raise BuiltInHostError(
                "claude_code_output_not_json", failure_kind="unknown", turn_usage=usage,
            )
        raise BuiltInHostError(f"claude_code_{kind}", failure_kind=kind, turn_usage=usage)
    result = _structured_result(envelope)
    if result is None:
        raise BuiltInHostError("claude_code_final_result_missing", turn_usage=usage)
    if not isinstance(result, dict):
        raise BuiltInHostError("claude_code_final_result_not_object", turn_usage=usage)
    if (
        result.get("schema_version") != LOOPX_TURN_RESULT_SCHEMA_VERSION
        or str(result.get("turn_key") or "") != turn_key
        or result.get("completed_phases") != list(TRANSACTION_PHASES[:2])
    ):
        raise BuiltInHostError(
            "claude_code_result_contract_mismatch",
            failure_kind="contract_rejected",
            turn_usage=usage,
        )
    return attach_turn_usage(result, usage)
