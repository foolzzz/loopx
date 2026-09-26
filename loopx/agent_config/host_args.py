"""Translate provider/agent definitions into explicit host launch arguments.

Everything here is argv or environment *names*; no secret value is read except
by :func:`provider_launch_env`, which resolves one credential into the child
environment in memory only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping

from .agents import AgentDefinition
from .codex_config import normalize_codex_config_override
from .errors import AgentConfigError
from .providers import Provider

def _toml_string(value: str) -> str:
    return json.dumps(value)


def codex_provider_config_overrides(provider: Provider) -> list[str]:
    """Return the ``KEY=VALUE`` overrides that point Codex at a provider.

    ``openai`` uses Codex's own configuration and needs none. ``codex-cpa`` and
    ``openai-compatible`` become an explicit ``model_providers.<id>`` table so
    the user's ``~/.codex/config.toml`` is neither needed nor modified.
    """

    if provider.kind in {"openai", "anthropic"}:
        return []
    provider_id = provider.model_provider_id or "cpa"
    prefix = f"model_providers.{provider_id}"
    overrides = [
        f"model_provider={_toml_string(provider_id)}",
        f"{prefix}.name={_toml_string(provider.display_name or provider_id)}",
        f"{prefix}.base_url={_toml_string(provider.base_url or '')}",
        f"{prefix}.wire_api={_toml_string(provider.wire_api or 'responses')}",
    ]
    if provider.auth.type in {"api_key", "oauth_token"} and provider.auth.env:
        overrides.append(f"{prefix}.env_key={_toml_string(provider.auth.env)}")
    overrides.append(f"{prefix}.requires_openai_auth=false")
    if provider.service_tier:
        overrides.append(f"service_tier={_toml_string(provider.service_tier)}")
    return [normalize_codex_config_override(item) for item in overrides]


def _keychain_secret(provider: Provider, environ: Mapping[str, str]) -> str | None:
    keychain = provider.auth.keychain
    if keychain is None:
        return None
    security = shutil.which("security", path=environ.get("PATH")) or "/usr/bin/security"
    argv = [security, "find-generic-password", "-s", keychain.service, "-w"]
    if keychain.account:
        argv[4:4] = ["-a", keychain.account]
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    secret = (completed.stdout or "").strip()
    return secret if completed.returncode == 0 and secret else None


def provider_launch_env(
    provider: Provider,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the extra child env a host needs for this provider.

    The returned mapping may contain a secret value; callers must hand it only
    to the child process environment and never log or persist it.
    """

    environ = os.environ if environ is None else environ
    extra: dict[str, str] = {}
    auth = provider.auth
    if auth.type == "oauth_cli":
        return extra
    value = str(environ.get(auth.env or "", "")).strip() if auth.env else ""
    if not value and auth.type == "api_key":
        value = _keychain_secret(provider, environ) or ""
    if not value:
        raise AgentConfigError(
            [f"provider {provider.name!r}: credential unavailable (run `loopx provider check`)"]
        )
    if provider.kind == "anthropic":
        target = "CLAUDE_CODE_OAUTH_TOKEN" if auth.type == "oauth_token" else "ANTHROPIC_API_KEY"
        extra[target] = value
        if provider.base_url:
            extra["ANTHROPIC_BASE_URL"] = provider.base_url
    elif provider.kind == "openai":
        extra[auth.env or "OPENAI_API_KEY"] = value
    else:
        # codex-cpa / openai-compatible read the key named by env_key.
        extra[auth.env or "CPA_API_KEY"] = value
    return extra


def turn_run_once_host_arguments(agent: AgentDefinition) -> list[str]:
    """Map one agent definition to ``loopx turn run-once`` host flags."""

    if agent.runtime == "claude-code":
        argv: list[str] = ["--host", "claude-code"]
        if agent.model:
            argv.extend(["--claude-model", agent.model])
        if agent.permission_mode:
            argv.extend(["--claude-permission-mode", agent.permission_mode])
        if agent.reasoning_effort:
            argv.extend(["--claude-effort", agent.reasoning_effort])
        if agent.system_prompt_file:
            argv.extend(["--claude-system-prompt-file", agent.system_prompt_file])
        for item in agent.extra_args:
            argv.append(f"--claude-extra-arg={item}")
        return argv
    argv = ["--host", "codex-cli"]
    if agent.model:
        argv.extend(["--codex-model", agent.model])
    if agent.reasoning_effort:
        argv.extend(["--codex-reasoning-effort", agent.reasoning_effort])
    if agent.sandbox:
        argv.extend(["--codex-sandbox", agent.sandbox])
    if agent.provider_config is not None:
        for item in codex_provider_config_overrides(agent.provider_config):
            argv.append(f"--codex-config={item}")
    for item in agent.extra_args:
        # codex-cli extra_args are validated KEY=VALUE config overrides.
        argv.append(f"--codex-config={item}")
    return argv
