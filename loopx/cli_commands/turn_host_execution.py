"""Bind Turn CLI host options without owning execution or settlement policy."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..control_plane.turn_driver import (
    build_loopx_turn_command_validator,
    codex_cli_session_binding,
    run_codex_cli_host,
)
from ..control_plane.turn_driver.command_validation import TaskValidator
from ..control_plane.turn_driver.host_binding import managed_executor_binding
from .turn_claude_host import build_claude_code_host_runner, resolve_codex_config_overrides
from .turn_dsh_host import build_dsh_host_runner

HostRunner = Callable[[Mapping[str, Any]], dict[str, Any]]
SessionBindingResolver = Callable[[Mapping[str, Any]], dict[str, str] | None]


def project_turn_executor_binding(
    args: argparse.Namespace, *, environ: Mapping[str, str],
) -> dict[str, Any]:
    """Project the selected host using the same machine environment as launch."""
    return managed_executor_binding(
        args.host,
        # The credential a managed Turn authenticates with is this
        # machine's resolved pair, not whatever the invoking shell happens
        # to export: the readback above the launch and the launch itself
        # have to name the same credential.
        environ=environ,
        dsh_runner_configured=bool(getattr(args, "dsh_runner", None)),
        provider=(
            getattr(args, "dsh_provider", None)
            if args.host == "dsh"
            else None
        ),
        model=(
            getattr(args, "dsh_model", None)
            if args.host == "dsh"
            else getattr(args, "codex_model", None)
            if args.host == "codex-cli"
            else getattr(args, "claude_model", None)
            if args.host == "claude-code"
            else None
        ),
        reasoning_effort=(
            getattr(args, "dsh_reasoning_effort", None)
            if args.host == "dsh"
            else getattr(args, "codex_reasoning_effort", None)
            if args.host == "codex-cli"
            else getattr(args, "claude_effort", None)
            if args.host == "claude-code"
            else None
        ),
        max_tokens=(
            getattr(args, "dsh_max_tokens", None)
            if args.host == "dsh"
            else None
        ),
    )


def resolve_turn_host_commands(
    args: argparse.Namespace, *, project: Path,
) -> tuple[list[str] | None, TaskValidator | None]:
    """Parse host and validator argv in order, without executing either."""
    if args.host == "generic-cli":
        if not args.host_command_json:
            raise ValueError(
                "generic-cli requires --host-adapter-command-json "
                "(alias --host-command-json)"
            )
        raw_argv = json.loads(args.host_command_json)
        if not isinstance(raw_argv, list) or not all(
            isinstance(item, str) for item in raw_argv
        ):
            raise ValueError(
                "--host-adapter-command-json must be a JSON string array"
            )
    else:
        if args.host_command_json:
            raise ValueError(
                f"{args.host} does not accept --host-command-json"
            )
        raw_argv = None
    if args.validation_command_json:
        raw_validation_argv = json.loads(args.validation_command_json)
        if not isinstance(raw_validation_argv, list) or not all(
            isinstance(item, str) for item in raw_validation_argv
        ):
            raise ValueError(
                "--validation-command-json must be a JSON string array"
            )
        task_validator = build_loopx_turn_command_validator(
            raw_validation_argv,
            project=project,
            timeout_seconds=args.validation_timeout_seconds,
            failure_recovery_kind=args.validation_failure_kind,
        )
    else:
        task_validator = None
    return raw_argv, task_validator


def build_turn_host_callbacks(
    args: argparse.Namespace,
    *,
    runtime_root: Path,
    project: Path,
    environ: Mapping[str, str],
) -> tuple[HostRunner | None, SessionBindingResolver | None]:
    """Bind provider options; the Turn driver owns whether callbacks execute."""
    host_runner: HostRunner | None = None
    session_binding_resolver: SessionBindingResolver | None = None
    if args.host == "codex-cli":
        codex_config_overrides = resolve_codex_config_overrides(
            args, runtime_root=runtime_root
        )

        def run_built_in_host(
            request: Mapping[str, Any],
        ) -> dict[str, Any]:
            return run_codex_cli_host(
                request,
                runtime_root=runtime_root,
                project=project,
                codex_bin=args.codex_bin,
                sandbox=args.codex_sandbox,
                model=args.codex_model,
                reasoning_effort=args.codex_reasoning_effort,
                mcp_server=args.codex_mcp_server_json,
                config_overrides=codex_config_overrides,
                timeout_seconds=max(1.0, args.timeout_seconds - 5.0),
            )

        host_runner = run_built_in_host

        def resolve_built_in_session_binding(
            turn_envelope: Mapping[str, Any],
        ) -> dict[str, str] | None:
            return codex_cli_session_binding(runtime_root, turn_envelope)

        session_binding_resolver = resolve_built_in_session_binding
    elif args.host == "claude-code":
        host_runner = build_claude_code_host_runner(
            args,
            project=project,
            runtime_root=runtime_root,
        )
    elif args.host == "dsh":
        host_runner = build_dsh_host_runner(
            args,
            workspace=project,
            environ=environ,
        )

    return host_runner, session_binding_resolver
