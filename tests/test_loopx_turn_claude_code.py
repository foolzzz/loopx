from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

from loopx.cli import main as cli_main
from loopx.control_plane.turn_driver.claude_code import (
    claude_code_result_schema,
    run_claude_code_host,
)
from loopx.control_plane.turn_driver.host_failure import BuiltInHostError
from tests.test_loopx_turn_codex_cli import _request
from tests.test_loopx_turn_driver import _write_live_fixture

FAKE_CLAUDE = """
import json, os, pathlib, re, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "argv": args,
            "cwd": os.getcwd(),
            "token_present": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
        }) + "\\n")
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
match = re.search(r'"turn_key":"([^"]+)"', prompt)
turn_key = match.group(1) if match else ""
if mode == "sleep":
    time.sleep(30)
if mode == "write":
    pathlib.Path("fixture-artifact.txt").write_text("validated", encoding="utf-8")
result = {
    "schema_version": "loopx_turn_result_v0",
    "turn_key": turn_key if mode != "mismatch" else "sha256:" + "f" * 64,
    "result_kind": "validated_progress",
    "completed_phases": ["host_execute", "typed_result"],
    "classification": "fixture_progress",
    "recommended_action": "Continue the public fixture",
    "next_action": "Run the next public fixture check",
    "delivery_batch_scale": "implementation",
    "delivery_outcome": "outcome_progress",
    "vision_unchanged_reason": "The fixture objective remains unchanged.",
    "path_delta_mode": "unchanged",
    "agent_vision_json": "",
    "summary": "One public fixture advanced.",
    "reward_memory_reflection_json": "",
}
envelope = {"type": "result", "subtype": "success", "is_error": False,
            "api_error_status": None, "session_id": "s-1",
            "result": json.dumps(result), "structured_output": result}
if mode == "result_text_only":
    envelope.pop("structured_output")
if mode == "rate_limited":
    envelope.update(is_error=True, subtype="error_during_execution",
                    api_error_status=429, result="API Error: 429", structured_output=None)
    print(json.dumps(envelope)); raise SystemExit(1)
if mode == "auth":
    print("Invalid API key · Please run /login", file=sys.stderr)
    raise SystemExit(1)
if mode == "schema_retries":
    envelope.update(is_error=True, subtype="error_max_structured_output_retries",
                    structured_output=None, result="")
    print(json.dumps(envelope)); raise SystemExit(0)
if mode == "not_json":
    print("plain text"); raise SystemExit(0)
if mode == "not_object":
    envelope["structured_output"] = [1, 2]
print(json.dumps(envelope))
"""


def _fake_claude(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "bin" / "claude"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}", encoding="utf-8")
    executable.chmod(0o755)
    return executable, tmp_path / "claude-argv.jsonl"


@pytest.fixture()
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    executable, log_path = _fake_claude(tmp_path)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log_path))
    project = tmp_path / "project"
    project.mkdir()
    return executable, log_path, project


def _log(log_path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_claude_code_host_success_builds_expected_command(
    fake_claude: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    executable, log_path, project = fake_claude
    prompt_file = tmp_path / "developer.md"
    prompt_file.write_text("be careful\n", encoding="utf-8")
    request = _request()

    result = run_claude_code_host(
        request,
        project=project,
        claude_bin=str(executable),
        model="haiku",
        permission_mode="acceptEdits",
        reasoning_effort="high",
        system_prompt_file=prompt_file,
        extra_args=["--verbose"],
        env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"},
        timeout_seconds=10,
    )

    assert result["turn_key"] == request["turn_key"]
    assert result["completed_phases"] == ["host_execute", "typed_result"]
    row = _log(log_path)[0]
    argv = row["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"]
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema == claude_code_result_schema(request)
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--append-system-prompt-file") + 1] == str(prompt_file)
    assert "--no-session-persistence" in argv
    assert argv[-1] == "--verbose"
    assert row["cwd"] == str(project.resolve())
    assert row["token_present"] is True


def test_claude_code_host_accepts_result_text_without_structured_output(
    fake_claude: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, _, project = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "result_text_only")
    result = run_claude_code_host(
        _request(), project=project, claude_bin=str(executable), timeout_seconds=10
    )
    assert result["result_kind"] == "validated_progress"


@pytest.mark.parametrize(
    ("mode", "reason", "failure_kind"),
    [
        ("rate_limited", "claude_code_rate_limited", "rate_limited"),
        ("auth", "claude_code_auth_failed", "auth_failed"),
        ("schema_retries", "claude_code_contract_rejected", "contract_rejected"),
        ("not_json", "claude_code_output_not_json", "unknown"),
        ("not_object", "claude_code_final_result_not_object", "unknown"),
        ("mismatch", "claude_code_result_contract_mismatch", "contract_rejected"),
    ],
)
def test_claude_code_host_classifies_failures(
    fake_claude: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    reason: str,
    failure_kind: str,
) -> None:
    executable, _, project = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    with pytest.raises(BuiltInHostError) as exc_info:
        run_claude_code_host(
            _request(), project=project, claude_bin=str(executable), timeout_seconds=10
        )
    assert exc_info.value.reason == reason
    assert exc_info.value.failure_kind == failure_kind
    # Provider prose never becomes the public reason.
    assert "Invalid API key" not in str(exc_info.value)


def test_claude_code_host_times_out(
    fake_claude: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, _, project = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    with pytest.raises(BuiltInHostError) as exc_info:
        run_claude_code_host(
            _request(), project=project, claude_bin=str(executable), timeout_seconds=1
        )
    assert exc_info.value.failure_kind == "executor_timeout"


@pytest.mark.parametrize(
    ("kwargs", "request_overrides", "message"),
    [
        ({"permission_mode": "yolo"}, {}, "permission mode"),
        ({"reasoning_effort": "ultra"}, {}, "effort"),
        ({"extra_args": ["--output-format=text"]}, {}, "reserved"),
        ({"extra_args": ["--resume"]}, {}, "reserved"),
        ({"system_prompt_file": "/nonexistent/prompt.md"}, {}, "system prompt file"),
        ({}, {"session_action": "resume"}, "resume is unsupported"),
        ({"claude_bin": "/nonexistent/claude"}, {}, "unavailable"),
    ],
)
def test_claude_code_host_rejects_bad_configuration_before_launch(
    fake_claude: tuple[Path, Path, Path],
    kwargs: dict[str, object],
    request_overrides: dict[str, str],
    message: str,
) -> None:
    executable, log_path, project = fake_claude
    call = {"claude_bin": str(executable), **kwargs}
    with pytest.raises(ValueError, match=message):
        run_claude_code_host(
            _request(**request_overrides), project=project, timeout_seconds=5, **call
        )
    assert not log_path.exists()


def _run_cli(argv: list[str]) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli_main(argv)
    return code, json.loads(output.getvalue())


def test_turn_run_once_cli_commits_claude_code_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, runtime, registry = _write_live_fixture(tmp_path)
    executable, log_path = _fake_claude(tmp_path)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log_path))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "write")
    monkeypatch.setenv("FIXTURE_OAUTH_TOKEN", "token-value")
    (runtime / "providers.yaml").write_text(
        "providers:\n  anthropic-token:\n    kind: anthropic\n"
        "    auth: {type: oauth_token, env: FIXTURE_OAUTH_TOKEN}\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    validation_script = (
        "import json, pathlib, sys\n"
        "json.load(sys.stdin)\n"
        "raise SystemExit(0 if pathlib.Path('fixture-artifact.txt').read_text() == 'validated' else 7)\n"
    )

    code, payload = _run_cli(
        [
            "--registry", str(registry), "--runtime-root", str(runtime), "--format", "json",
            "turn", "run-once", "--host", "claude-code",
            "--goal-id", "loopx-turn-fixture", "--agent-id", "codex-fixture",
            "--project", str(workspace),
            "--claude-bin", str(executable), "--claude-model", "haiku",
            "--claude-permission-mode", "acceptEdits", "--claude-effort", "low",
            "--claude-provider", "anthropic-token",
            "--validation-command-json", json.dumps([sys.executable, "-c", validation_script]),
            "--scan-root", str(project), "--no-global-sync", "--execute",
        ]
    )

    assert code == 0, payload
    assert payload["status"] == "committed"
    assert payload["validation"]["status"] == "passed"
    assert payload["effects"]["host_invoked"] is True
    assert payload["managed_executor"]["executor"] == "claude-code"
    assert payload["managed_executor"]["execution_profile"] == "haiku@low"
    row = _log(log_path)[0]
    assert row["cwd"] == str(workspace.resolve())
    assert row["token_present"] is True
    assert "token-value" not in json.dumps(payload)


def test_turn_run_once_cli_records_claude_code_host_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, runtime, registry = _write_live_fixture(tmp_path)
    executable, log_path = _fake_claude(tmp_path)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log_path))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "rate_limited")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    code, payload = _run_cli(
        [
            "--registry", str(registry), "--runtime-root", str(runtime), "--format", "json",
            "turn", "run-once", "--host", "claude-code",
            "--goal-id", "loopx-turn-fixture", "--agent-id", "codex-fixture",
            "--project", str(workspace), "--claude-bin", str(executable),
            "--scan-root", str(project), "--no-global-sync", "--execute",
        ]
    )

    assert code == 1
    assert payload["effects"]["host_invoked"] is True
    assert payload["effects"]["quota_spent"] is False
    assert payload["host_failure"]["kind"] == "rate_limited"
    assert payload["host_failure"]["retryable"] is True
