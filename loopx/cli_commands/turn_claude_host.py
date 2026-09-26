"""Bind CLI options to the built-in claude-code host and Codex config overrides."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..agent_config import (
    AgentConfigError,
    Provider,
    codex_provider_config_overrides,
    load_providers,
    normalize_codex_config_override,
    provider_launch_env,
)
from ..control_plane.turn_driver.claude_code import run_claude_code_host
from ..control_plane.turn_driver.host_failure import BuiltInHostError

HostRunner = Callable[[Mapping[str, Any]], dict[str, Any]]


def _named_provider(runtime_root: Path, name: str, *, allowed_kinds: tuple[str, ...]) -> Provider:
    providers = load_providers(runtime_root)
    provider = providers.get(name)
    if provider is None:
        raise ValueError(f"provider {name!r} is not defined in {runtime_root}/providers.yaml")
    if provider.kind not in allowed_kinds:
        raise ValueError(
            f"provider {name!r} has kind {provider.kind}; expected {', '.join(allowed_kinds)}"
        )
    return provider


def resolve_codex_config_overrides(
    args: argparse.Namespace,
    *,
    runtime_root: Path,
) -> list[str]:
    """Provider-derived overrides first, then explicit ``--codex-config`` items."""

    overrides: list[str] = []
    provider_name = getattr(args, "codex_provider", None)
    if provider_name:
        provider = _named_provider(
            runtime_root,
            provider_name,
            allowed_kinds=("openai", "openai-compatible", "codex-cpa"),
        )
        overrides.extend(codex_provider_config_overrides(provider))
    for item in getattr(args, "codex_config", None) or []:
        overrides.append(normalize_codex_config_override(item))
    return overrides


def build_claude_code_host_runner(
    args: argparse.Namespace,
    *,
    project: Path,
    runtime_root: Path,
) -> HostRunner:
    provider: Provider | None = None
    if getattr(args, "claude_provider", None):
        provider = _named_provider(
            runtime_root, args.claude_provider, allowed_kinds=("anthropic",)
        )
    system_prompt_file = (
        Path(args.claude_system_prompt_file).expanduser().resolve()
        if args.claude_system_prompt_file
        else None
    )
    if system_prompt_file is not None and not system_prompt_file.is_file():
        raise ValueError("--claude-system-prompt-file does not exist")

    def run(request: Mapping[str, Any]) -> dict[str, Any]:
        env: dict[str, str] = {}
        if provider is not None:
            try:
                env = provider_launch_env(provider)
            except AgentConfigError as exc:
                raise BuiltInHostError(
                    "claude_code_credential_unavailable",
                    failure_kind="auth_failed",
                ) from exc
        return run_claude_code_host(
            request,
            project=project,
            claude_bin=args.claude_bin,
            model=args.claude_model,
            permission_mode=args.claude_permission_mode,
            reasoning_effort=args.claude_effort,
            system_prompt_file=system_prompt_file,
            extra_args=list(args.claude_extra_arg or []),
            env=env,
            timeout_seconds=max(1.0, args.timeout_seconds - 5.0),
        )

    return run
