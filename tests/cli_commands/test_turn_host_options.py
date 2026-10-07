"""Turn host option diagnostics and readback remain CLI-owned and read-only."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from loopx.cli import main
from tests.test_loopx_turn_driver import _write_live_fixture


def _invoke(root: Path, action: str, options: list[str]) -> tuple[int, dict]:
    project, runtime, registry = _write_live_fixture(root)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main([
            "--registry", str(registry), "--runtime-root", str(runtime),
            "--format", "json", "turn", action,
            "--goal-id", "loopx-turn-fixture", "--agent-id", "codex-fixture",
            "--scan-root", str(project),
            *(["--project", str(project)] if action == "run-once" else []), *options,
        ])
    after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before
    return code, json.loads(output.getvalue())


@pytest.mark.parametrize(
    "options, error",
    [
        (["--host", "generic-cli"],
         "generic-cli requires --host-adapter-command-json (alias --host-command-json)"),
        (["--host", "generic-cli", "--host-command-json", "{}"],
         "--host-adapter-command-json must be a JSON string array"),
        (["--host", "generic-cli", "--host-command-json", '["echo", 1]'],
         "--host-adapter-command-json must be a JSON string array"),
        (["--host", "generic-cli", "--host-command-json", '"echo"'],
         "--host-adapter-command-json must be a JSON string array"),
        (["--host", "codex-cli", "--host-command-json", '["echo"]'],
         "codex-cli does not accept --host-command-json"),
        (["--host", "claude-code", "--host-command-json", '["echo"]'],
         "claude-code does not accept --host-command-json"),
        (["--host", "dsh", "--host-command-json", '["echo"]'],
         "dsh does not accept --host-command-json"),
        (["--host", "codex-cli", "--validation-command-json", "{}"],
         "--validation-command-json must be a JSON string array"),
        (["--host", "codex-cli", "--validation-command-json", '["echo", 1]'],
         "--validation-command-json must be a JSON string array"),
        (["--host", "codex-cli", "--validation-command-json", "[]"],
         "task validator command must contain at least one argv item"),
        (["--host", "generic-cli", "--validation-command-json", "{}"],
         "generic-cli requires --host-adapter-command-json (alias --host-command-json)"),
    ],
)
def test_run_once_rejects_invalid_host_options_without_writes(
    tmp_path: Path, options: list[str], error: str,
) -> None:
    code, payload = _invoke(tmp_path, "run-once", options)
    assert code == 1
    assert payload["ok"] is False
    assert payload["error"] == error
    assert payload["effects"]["host_invoked"] is False


@pytest.mark.parametrize(
    "host, options, profile",
    [
        ("codex-cli", ["--codex-model", "fixture-model", "--codex-reasoning-effort", "high"],
         "fixture-model@high"),
        ("claude-code", ["--claude-model", "fixture-model", "--claude-effort", "high"],
         "fixture-model@high"),
        ("dsh", ["--dsh-provider", "fixture-provider", "--dsh-model", "fixture-model",
                 "--dsh-reasoning-effort", "high", "--dsh-max-tokens", "5000"],
         "fixture-provider/fixture-model@high"),
        ("generic-cli", ["--host-command-json", '["echo"]'], None),
    ],
)
def test_run_once_dry_run_projects_only_the_selected_host_profile(
    tmp_path: Path, host: str, options: list[str], profile: str | None,
) -> None:
    code, payload = _invoke(tmp_path, "run-once", ["--host", host, *options])
    assert code == 0, payload
    assert payload["managed_executor"]["executor"] == host
    assert payload["managed_executor"]["execution_profile"] == profile
    if host == "dsh":
        assert payload["managed_executor"]["output_token_budget"]["max_tokens"] == 5000
    else:
        assert "output_token_budget" not in payload["managed_executor"]
