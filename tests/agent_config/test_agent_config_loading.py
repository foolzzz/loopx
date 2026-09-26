from __future__ import annotations

from pathlib import Path

import pytest

from loopx.agent_config import (
    AgentConfigError,
    codex_config_arguments,
    codex_provider_config_overrides,
    load_agent_definitions,
    load_providers,
    normalize_codex_config_override,
    provider_launch_env,
    resolve_agent,
    turn_run_once_host_arguments,
)

PROVIDERS = """
providers:
  - name: anthropic-login
    kind: anthropic
    auth: {type: oauth_cli}
  - name: anthropic-token
    kind: anthropic
    auth: {type: oauth_token, env: CLAUDE_CODE_OAUTH_TOKEN}
  - name: anthropic-key
    kind: anthropic
    base_url: https://gateway.example.test
    auth:
      type: api_key
      env: FIXTURE_ANTHROPIC_KEY
      keychain: {service: fixture-anthropic, account: me}
  - name: cpa
    kind: codex-cpa
    auth: {type: api_key}
  - name: local-llm
    kind: openai-compatible
    base_url: http://127.0.0.1:9999/v1
    wire_api: chat
    auth: {type: api_key, env: LOCAL_LLM_KEY}
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    project = tmp_path / "project"
    _write(runtime / "providers.yaml", PROVIDERS)
    _write(runtime / "prompts" / "dev.md", "global developer prompt\n")
    _write(
        runtime / "agents" / "dev.yaml",
        """
role: developer
runtime: claude-code
provider: anthropic-login
model: opus
reasoning_effort: high
system_prompt_file: ../prompts/dev.md
permission_mode: acceptEdits
max_concurrency: 2
extra_args: ["--verbose"]
""",
    )
    _write(
        runtime / "agents" / "acceptor.yaml",
        """
id: acceptor
role: acceptor
runtime: codex-cli
provider: cpa
model: gpt-5.6-sol
reasoning_effort: xhigh
""",
    )
    return runtime, project


def test_providers_parse_with_kind_defaults(roots: tuple[Path, Path]) -> None:
    runtime, _ = roots
    providers = load_providers(runtime)

    assert set(providers) == {
        "anthropic-login",
        "anthropic-token",
        "anthropic-key",
        "cpa",
        "local-llm",
    }
    assert providers["anthropic-login"].auth.cli == "claude"
    cpa = providers["cpa"]
    assert cpa.base_url == "http://127.0.0.1:8317/v1"
    assert cpa.auth.env == "CPA_API_KEY"
    assert cpa.model_provider_id == "cpa"
    assert providers["anthropic-key"].auth.keychain.account == "me"
    assert providers["local-llm"].model_provider_id == "local-llm"


def test_missing_provider_file_means_no_providers(tmp_path: Path) -> None:
    assert load_providers(tmp_path) == {}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "providers:\n  - {name: x, kind: anthropic, auth: {type: api_key, api_key: sk-live}}\n",
            "looks like a secret value",
        ),
        (
            "providers:\n  - {name: x, kind: anthropic, token: abc, auth: {type: oauth_cli}}\n",
            "looks like a secret value",
        ),
        ("providers:\n  - {name: x, kind: bedrock, auth: {type: oauth_cli}}\n", "kind must be one of"),
        ("providers:\n  - {name: x, kind: anthropic, auth: {type: api_key}}\n", "needs auth.env"),
        ("providers:\n  - {name: x, kind: anthropic, auth: {type: oauth_token}}\n", "needs auth.env"),
        ("providers:\n  - {name: x, kind: openai-compatible, auth: {type: api_key, env: K}}\n", "need base_url"),
        ("providers:\n  - {name: x, kind: anthropic, wire_api: chat, auth: {type: oauth_cli}}\n", "does not apply"),
        (
            "providers:\n  - {name: x, kind: anthropic, auth: {type: oauth_cli}}\n"
            "  - {name: x, kind: anthropic, auth: {type: oauth_cli}}\n",
            "duplicate provider name",
        ),
        ("providers: [\n", "invalid YAML"),
        ("other: 1\n", "top-level `providers`"),
    ],
)
def test_provider_validation_errors(tmp_path: Path, body: str, message: str) -> None:
    _write(tmp_path / "providers.yaml", body)
    with pytest.raises(AgentConfigError) as exc_info:
        load_providers(tmp_path)
    assert message in str(exc_info.value)
    # The rejected literal never appears in the error text.
    assert "sk-live" not in str(exc_info.value)


def test_resolve_agent_resolves_prompt_relative_to_yaml(roots: tuple[Path, Path]) -> None:
    runtime, project = roots
    agent = resolve_agent("dev", project, runtime_root=runtime)

    assert agent.role == "developer"
    assert agent.runtime == "claude-code"
    assert agent.system_prompt_file == str((runtime / "prompts" / "dev.md").resolve())
    assert agent.provider_config is not None and agent.provider_config.kind == "anthropic"
    assert agent.max_concurrency == 2
    assert agent.extra_args == ("--verbose",)
    assert agent.sources == (str(runtime / "agents" / "dev.yaml"),)


def test_project_overrides_global_field_by_field(roots: tuple[Path, Path]) -> None:
    runtime, project = roots
    _write(project / ".loopx" / "agents" / "prompts" / "dev.md", "project prompt\n")
    _write(
        project / ".loopx" / "agents" / "dev.yaml",
        "model: sonnet\nsystem_prompt_file: prompts/dev.md\nextra_args: []\n",
    )

    agent = resolve_agent("dev", project, runtime_root=runtime)

    assert agent.model == "sonnet"
    assert agent.system_prompt_file == str(
        (project / ".loopx" / "agents" / "prompts" / "dev.md").resolve()
    )
    # Untouched fields come from the global layer; lists are replaced wholesale.
    assert agent.reasoning_effort == "high"
    assert agent.permission_mode == "acceptEdits"
    assert agent.extra_args == ()
    assert len(agent.sources) == 2
    # Without --project the global definition is unchanged.
    assert resolve_agent("dev", runtime_root=runtime).model == "opus"


def test_project_only_agent_is_resolved(roots: tuple[Path, Path]) -> None:
    runtime, project = roots
    _write(
        project / ".loopx" / "agents" / "orch.yaml",
        "role: orchestrator\nruntime: claude-code\nprovider: anthropic-token\n",
    )
    agent = resolve_agent("orch", project, runtime_root=runtime)
    assert agent.permission_mode == "dontAsk"
    assert agent.enabled is True


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("role: boss\nruntime: claude-code\nprovider: anthropic-login\n", "role must be one of"),
        ("role: developer\nruntime: gemini\nprovider: anthropic-login\n", "runtime must be one of"),
        ("role: developer\nruntime: claude-code\nprovider: nope\n", "is not defined in providers.yaml"),
        ("role: developer\nruntime: claude-code\nprovider: cpa\n", "cannot use provider kind codex-cpa"),
        ("role: developer\nruntime: codex-cli\nprovider: anthropic-login\n", "cannot use provider kind anthropic"),
        ("role: developer\nruntime: claude-code\nprovider: anthropic-login\nsandbox: read-only\n", "sandbox applies to codex-cli"),
        ("role: developer\nruntime: codex-cli\nprovider: cpa\npermission_mode: plan\n", "permission_mode applies to claude-code"),
        ("role: developer\nruntime: claude-code\nprovider: anthropic-login\nreasoning_effort: ultra\n", "reasoning_effort for claude-code"),
        ("role: developer\nruntime: claude-code\nprovider: anthropic-login\nmax_concurrency: 0\n", "max_concurrency"),
        ("role: developer\nruntime: claude-code\nprovider: anthropic-login\nenabled: yes-please\n", "enabled must be"),
        ("role: developer\nruntime: claude-code\nprovider: anthropic-login\nsystem_prompt_file: missing.md\n", "does not exist"),
        ("role: developer\nruntime: codex-cli\nprovider: cpa\nextra_args: ['--yolo']\n", "KEY=VALUE"),
        ("role: developer\nruntime: claude-code\nprovider: anthropic-login\ncolour: blue\n", "unsupported agent fields: colour"),
        ("id: other\nrole: developer\nruntime: claude-code\nprovider: anthropic-login\n", "does not match file name"),
        ("role: [\n", "invalid YAML"),
    ],
)
def test_agent_validation_errors(
    roots: tuple[Path, Path], body: str, message: str
) -> None:
    runtime, project = roots
    _write(project / ".loopx" / "agents" / "bad.yaml", body)

    with pytest.raises(AgentConfigError) as exc_info:
        resolve_agent("bad" if "id: other" not in body else "other", project, runtime_root=runtime)
    assert message in str(exc_info.value)
    assert "bad.yaml" in str(exc_info.value)

    valid, invalid, provider_issues = load_agent_definitions(project, runtime_root=runtime)
    assert set(valid) == {"dev", "acceptor"}
    assert len(invalid) == 1
    assert provider_issues == []


def test_unknown_agent_names_search_paths(roots: tuple[Path, Path]) -> None:
    runtime, project = roots
    with pytest.raises(AgentConfigError, match="is not defined"):
        resolve_agent("ghost", project, runtime_root=runtime)


def test_invalid_providers_file_invalidates_agents(roots: tuple[Path, Path]) -> None:
    runtime, project = roots
    _write(runtime / "providers.yaml", "providers: 3\n")
    valid, invalid, provider_issues = load_agent_definitions(project, runtime_root=runtime)
    assert valid == {}
    assert set(invalid) == {"dev", "acceptor"}
    assert provider_issues


def test_codex_cpa_provider_expands_to_explicit_overrides(roots: tuple[Path, Path]) -> None:
    runtime, _ = roots
    cpa = load_providers(runtime)["cpa"]

    assert codex_config_arguments(codex_provider_config_overrides(cpa)) == [
        "-c", 'model_provider="cpa"',
        "-c", 'model_providers.cpa.name="CPA"',
        "-c", 'model_providers.cpa.base_url="http://127.0.0.1:8317/v1"',
        "-c", 'model_providers.cpa.wire_api="responses"',
        "-c", 'model_providers.cpa.env_key="CPA_API_KEY"',
        "-c", "model_providers.cpa.requires_openai_auth=false",
        "-c", 'service_tier="default"',
    ]


def test_openai_compatible_provider_overrides(roots: tuple[Path, Path]) -> None:
    runtime, _ = roots
    provider = load_providers(runtime)["local-llm"]
    overrides = codex_provider_config_overrides(provider)
    assert overrides[0] == 'model_provider="local-llm"'
    assert 'model_providers.local-llm.wire_api="chat"' in overrides
    assert 'model_providers.local-llm.env_key="LOCAL_LLM_KEY"' in overrides
    assert not any(item.startswith("service_tier") for item in overrides)


@pytest.mark.parametrize(
    "value",
    ["no-equals", "=x", "bad key=1", "a..b=1", "--flag=1", "k=", "k=line\nbreak"],
)
def test_codex_config_override_rejects_malformed(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_codex_config_override(value)


def test_codex_config_override_accepts_toml_values() -> None:
    assert normalize_codex_config_override(' model="o3"') == 'model="o3"'
    assert normalize_codex_config_override("a.b-c.d_e=[1, 2]") == "a.b-c.d_e=[1, 2]"


def test_run_once_host_arguments_for_both_runtimes(roots: tuple[Path, Path]) -> None:
    runtime, project = roots
    dev = resolve_agent("dev", project, runtime_root=runtime)
    acceptor = resolve_agent("acceptor", project, runtime_root=runtime)

    assert turn_run_once_host_arguments(dev) == [
        "--host", "claude-code",
        "--claude-model", "opus",
        "--claude-permission-mode", "acceptEdits",
        "--claude-effort", "high",
        "--claude-system-prompt-file", dev.system_prompt_file,
        "--claude-extra-arg=--verbose",
    ]
    acceptor_args = turn_run_once_host_arguments(acceptor)
    assert acceptor_args[:8] == [
        "--host", "codex-cli",
        "--codex-model", "gpt-5.6-sol",
        "--codex-reasoning-effort", "xhigh",
        "--codex-sandbox", "read-only",
    ]
    assert '--codex-config=model_provider="cpa"' in acceptor_args


def test_provider_launch_env_maps_credentials_without_config_storage(
    roots: tuple[Path, Path],
) -> None:
    runtime, _ = roots
    providers = load_providers(runtime)

    assert provider_launch_env(providers["anthropic-login"], environ={}) == {}
    assert provider_launch_env(
        providers["anthropic-token"],
        environ={"CLAUDE_CODE_OAUTH_TOKEN": "tok"},
    ) == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
    assert provider_launch_env(
        providers["anthropic-key"],
        environ={"FIXTURE_ANTHROPIC_KEY": "k1"},
    ) == {"ANTHROPIC_API_KEY": "k1", "ANTHROPIC_BASE_URL": "https://gateway.example.test"}
    assert provider_launch_env(providers["cpa"], environ={"CPA_API_KEY": "c"}) == {
        "CPA_API_KEY": "c"
    }
    with pytest.raises(AgentConfigError, match="credential unavailable"):
        provider_launch_env(providers["anthropic-token"], environ={})
