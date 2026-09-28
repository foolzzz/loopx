"""Fork gap G12: role-based sandbox defaults (the acceptor runs unsandboxed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopx.agent_config import resolve_agent, turn_run_once_host_arguments

PROVIDERS = """
providers:
  claude-login: {kind: anthropic, auth: {type: oauth_cli}}
  codex-login: {kind: openai, auth: {type: oauth_cli}}
"""


def _agent(runtime: Path, agent_id: str, body: str) -> None:
    path = runtime / "agents" / f"{agent_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture()
def runtime(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "providers.yaml").write_text(PROVIDERS, encoding="utf-8")
    for role in ("orchestrator", "developer", "acceptor"):
        _agent(root, f"codex-{role}", f"role: {role}\nruntime: codex-cli\nprovider: codex-login\n")
        _agent(root, f"claude-{role}", f"role: {role}\nruntime: claude-code\nprovider: claude-login\n")
    return root


@pytest.mark.parametrize(
    ("role", "sandbox", "permission_mode"),
    [
        ("acceptor", "danger-full-access", "bypassPermissions"),
        ("developer", "read-only", "dontAsk"),
        ("orchestrator", "read-only", "dontAsk"),
    ],
)
def test_role_based_defaults(runtime: Path, role: str, sandbox: str, permission_mode: str) -> None:
    codex = resolve_agent(f"codex-{role}", runtime_root=runtime)
    claude = resolve_agent(f"claude-{role}", runtime_root=runtime)
    assert codex.sandbox == sandbox
    assert claude.permission_mode == permission_mode
    argv = turn_run_once_host_arguments(codex)
    assert argv[argv.index("--codex-sandbox") + 1] == sandbox
    argv = turn_run_once_host_arguments(claude)
    assert argv[argv.index("--claude-permission-mode") + 1] == permission_mode


def test_explicit_agent_config_overrides_the_acceptor_default(runtime: Path) -> None:
    _agent(runtime, "codex-acceptor",
           "role: acceptor\nruntime: codex-cli\nprovider: codex-login\nsandbox: workspace-write\n")
    _agent(runtime, "claude-acceptor",
           "role: acceptor\nruntime: claude-code\nprovider: claude-login\npermission_mode: acceptEdits\n")
    assert resolve_agent("codex-acceptor", runtime_root=runtime).sandbox == "workspace-write"
    assert resolve_agent("claude-acceptor", runtime_root=runtime).permission_mode == "acceptEdits"


def test_the_registry_role_selects_the_default(runtime: Path) -> None:
    # The dispatcher passes the registry role; it wins over the file's role for defaults.
    assert resolve_agent("codex-developer", runtime_root=runtime, role="acceptor").sandbox == "danger-full-access"
    assert resolve_agent("codex-acceptor", runtime_root=runtime, role="developer").sandbox == "read-only"
