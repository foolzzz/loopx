from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from loopx.cli_commands.turn_claude_host import resolve_codex_config_overrides
from loopx.control_plane.turn_driver.codex_cli import _codex_command, run_codex_cli_host
from tests.test_loopx_turn_codex_cli import _fake_codex, _request

CPA_ARGS = [
    'model_provider="cpa"',
    'model_providers.cpa.name="CPA"',
    'model_providers.cpa.base_url="http://127.0.0.1:8317/v1"',
    'model_providers.cpa.wire_api="responses"',
    'model_providers.cpa.env_key="CPA_API_KEY"',
    "model_providers.cpa.requires_openai_auth=false",
    'service_tier="default"',
]


def _config_values(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, value in enumerate(argv) if value == "-c"]


@pytest.mark.parametrize("session_id", [None, "session-fixture-0001"])
def test_codex_command_appends_config_overrides_for_fresh_and_resume(
    tmp_path: Path, session_id: str | None
) -> None:
    argv = _codex_command(
        codex_bin="codex",
        project=tmp_path,
        schema_path=tmp_path / "s.json",
        output_path=tmp_path / "o.json",
        sandbox="read-only",
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
        session_id=session_id,
        mcp_server=None,
        config_overrides=CPA_ARGS,
    )
    values = _config_values(argv)
    assert values[-len(CPA_ARGS):] == CPA_ARGS
    assert argv[-1] == "-"
    if session_id:
        assert argv[-2] == session_id


def test_run_codex_cli_host_passes_config_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, log_path = _fake_codex(tmp_path)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    project = tmp_path / "project"
    project.mkdir()

    run_codex_cli_host(
        _request(),
        runtime_root=tmp_path / "runtime",
        project=project,
        codex_bin=str(executable),
        config_overrides=CPA_ARGS,
        timeout_seconds=5,
    )

    argv = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    values = _config_values(argv)
    assert values == CPA_ARGS


def test_run_codex_cli_host_rejects_malformed_override_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, log_path = _fake_codex(tmp_path)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="KEY=VALUE"):
        run_codex_cli_host(
            _request(),
            runtime_root=tmp_path / "runtime",
            project=project,
            codex_bin=str(executable),
            config_overrides=["--dangerously-bypass"],
            timeout_seconds=5,
        )
    assert not log_path.exists()


def test_codex_provider_flag_expands_before_explicit_overrides(tmp_path: Path) -> None:
    (tmp_path / "providers.yaml").write_text(
        "providers:\n  cpa: {kind: codex-cpa, auth: {type: api_key}}\n"
        "  claude: {kind: anthropic, auth: {type: oauth_cli}}\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(codex_provider="cpa", codex_config=['model="gpt-5.6-sol"'])
    assert resolve_codex_config_overrides(args, runtime_root=tmp_path) == [
        *CPA_ARGS,
        'model="gpt-5.6-sol"',
    ]
    with pytest.raises(ValueError, match="expected openai"):
        resolve_codex_config_overrides(
            argparse.Namespace(codex_provider="claude", codex_config=[]),
            runtime_root=tmp_path,
        )
    with pytest.raises(ValueError, match="not defined"):
        resolve_codex_config_overrides(
            argparse.Namespace(codex_provider="ghost", codex_config=[]),
            runtime_root=tmp_path,
        )
