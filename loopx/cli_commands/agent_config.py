"""`loopx agent` and `loopx provider`: inspect agent/provider config files.

Read-only. Agent definitions come from ``<runtime-root>/agents/*.yaml`` and,
with ``--project``, ``<project>/.loopx/agents/*.yaml`` (project overrides
global per field). Providers come from ``<runtime-root>/providers.yaml``.
``provider check`` runs the auth preflight; secret values are never read into
the output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..agent_config import (
    AgentConfigError,
    check_provider,
    global_agents_dir,
    load_agent_definitions,
    load_providers,
    preflight_agent,
    project_agents_dir,
    providers_path,
    resolve_agent,
    turn_run_once_host_arguments,
)

AGENT_CONFIG_SCHEMA_VERSION = "loopx_agent_config_v0"
PROVIDER_CONFIG_SCHEMA_VERSION = "loopx_provider_config_v0"


def register_agent_config_commands(subparsers, add_format) -> None:
    agent = subparsers.add_parser(
        "agent",
        help="List, show or validate agent definition files (global + project).",
    )
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    for name, help_text in (
        ("list", "List merged agent definitions and their validity."),
        ("show", "Show one merged agent definition and its run-once host flags."),
        ("validate", "Validate agent files; exit 1 when any agent is invalid."),
    ):
        command = agent_sub.add_parser(name, help=help_text)
        add_format(command)
        command.add_argument(
            "--project",
            help="Project root whose .loopx/agents/*.yaml override global agents.",
        )
        if name == "show":
            command.add_argument("agent_id")
            command.add_argument(
                "--check-auth",
                action="store_true",
                help="Also run the agent's runtime + provider auth preflight.",
            )
        if name == "validate":
            command.add_argument("agent_id", nargs="?")
            command.add_argument(
                "--check-auth",
                action="store_true",
                help="Also run the auth preflight for every valid enabled agent.",
            )

    provider = subparsers.add_parser(
        "provider",
        help="List providers or run the auth preflight (never prints secrets).",
    )
    provider_sub = provider.add_subparsers(dest="provider_command", required=True)
    list_parser = provider_sub.add_parser("list", help="List configured providers.")
    add_format(list_parser)
    check = provider_sub.add_parser(
        "check",
        help="Check provider credentials are reachable (env, keychain or CLI login).",
    )
    add_format(check)
    check.add_argument("--name", help="Check only this provider.")
    check.add_argument("--timeout-seconds", type=float, default=15.0)


def _project(args) -> Path | None:
    value = getattr(args, "project", None)
    return Path(value).expanduser().resolve() if value else None


def _error_payload(schema: str, command: str, issues: list[str]) -> dict[str, Any]:
    return {
        "ok": False,
        "schema_version": schema,
        "command": command,
        "error_code": "invalid_config",
        "issues": issues,
    }


def handle_agent_config_command(args, *, runtime_root: Path, print_payload, output_format):
    if args.command == "agent":
        payload = _handle_agent(args, runtime_root=runtime_root)
        print_payload(payload, output_format(args), render_agent_config)
        return 0 if payload.get("ok") else 1
    if args.command == "provider":
        payload = _handle_provider(args, runtime_root=runtime_root)
        print_payload(payload, output_format(args), render_provider_config)
        return 0 if payload.get("ok") else 1
    return None


def _handle_agent(args, *, runtime_root: Path) -> dict[str, Any]:
    project = _project(args)
    command = f"agent {args.agent_command}"
    base = {
        "schema_version": AGENT_CONFIG_SCHEMA_VERSION,
        "command": command,
        "runtime_root": str(runtime_root),
        "search_paths": [
            str(global_agents_dir(runtime_root)),
            *([str(project_agents_dir(project))] if project else []),
        ],
    }
    if args.agent_command == "show":
        try:
            agent = resolve_agent(args.agent_id, project, runtime_root=runtime_root)
        except AgentConfigError as exc:
            return {**base, **_error_payload(AGENT_CONFIG_SCHEMA_VERSION, command, exc.issues)}
        payload = {
            **base,
            "ok": True,
            "agent": agent.to_dict(),
            "turn_run_once_host_args": turn_run_once_host_arguments(agent),
        }
        if args.check_auth:
            preflight = preflight_agent(agent)
            payload["preflight"] = preflight
            payload["ok"] = bool(preflight["ok"])
        return payload
    valid, invalid, provider_issues = load_agent_definitions(
        project, runtime_root=runtime_root
    )
    if args.agent_command == "validate" and args.agent_id:
        valid = {k: v for k, v in valid.items() if k == args.agent_id}
        invalid = {k: v for k, v in invalid.items() if k == args.agent_id}
        if not valid and not invalid:
            invalid = {args.agent_id: [f"agent {args.agent_id!r} is not defined"]}
    rows: list[dict[str, Any]] = []
    for agent_id in sorted({*valid, *invalid}):
        if agent_id in valid:
            agent = valid[agent_id]
            row = {
                "id": agent_id,
                "valid": True,
                "role": agent.role,
                "runtime": agent.runtime,
                "provider": agent.provider,
                "model": agent.model,
                "enabled": agent.enabled,
                "max_concurrency": agent.max_concurrency,
                "sources": list(agent.sources),
            }
            if args.agent_command == "validate" and getattr(args, "check_auth", False) and agent.enabled:
                row["preflight"] = preflight_agent(agent)
        else:
            row = {"id": agent_id, "valid": False, "issues": invalid[agent_id]}
        rows.append(row)
    ok = not provider_issues
    if args.agent_command == "validate":
        ok = ok and not invalid and all(
            (row.get("preflight") or {}).get("ok", True) for row in rows
        )
    return {
        **base,
        "ok": ok,
        "agent_count": len(rows),
        "invalid_count": len(invalid),
        "provider_issues": provider_issues,
        "agents": rows,
    }


def _handle_provider(args, *, runtime_root: Path) -> dict[str, Any]:
    command = f"provider {args.provider_command}"
    base = {
        "schema_version": PROVIDER_CONFIG_SCHEMA_VERSION,
        "command": command,
        "providers_file": str(providers_path(runtime_root)),
    }
    try:
        providers = load_providers(runtime_root)
    except AgentConfigError as exc:
        return {**base, **_error_payload(PROVIDER_CONFIG_SCHEMA_VERSION, command, exc.issues)}
    if args.provider_command == "list":
        return {
            **base,
            "ok": True,
            "provider_count": len(providers),
            "providers": [provider.to_dict() for provider in providers.values()],
        }
    selected = list(providers.values())
    if args.name:
        selected = [p for p in selected if p.name == args.name]
        if not selected:
            return {
                **base,
                "ok": False,
                "error_code": "provider_not_found",
                "issues": [f"provider {args.name!r} is not defined"],
            }
    checks = [
        check_provider(provider, timeout=max(1.0, args.timeout_seconds))
        for provider in selected
    ]
    return {
        **base,
        "ok": all(item["ok"] for item in checks),
        "provider_count": len(checks),
        "checks": checks,
    }


def render_agent_config(payload: dict[str, Any]) -> str:
    lines = [f"loopx {payload.get('command')}: {'ok' if payload.get('ok') else 'NOT OK'}"]
    for issue in payload.get("issues") or []:
        lines.append(f"- error: {issue}")
    for issue in payload.get("provider_issues") or []:
        lines.append(f"- providers.yaml: {issue}")
    agent = payload.get("agent")
    if isinstance(agent, dict):
        for key in (
            "id", "role", "runtime", "provider", "model", "reasoning_effort",
            "permission_mode", "sandbox", "max_concurrency", "enabled",
            "system_prompt_file",
        ):
            if agent.get(key) is not None:
                lines.append(f"- {key}: {agent[key]}")
        if agent.get("extra_args"):
            lines.append(f"- extra_args: {' '.join(agent['extra_args'])}")
        lines.append(f"- sources: {', '.join(agent.get('sources') or [])}")
        lines.append(
            "- run-once flags: " + " ".join(payload.get("turn_run_once_host_args") or [])
        )
    preflight = payload.get("preflight")
    if isinstance(preflight, dict):
        lines.append(f"- preflight: {preflight.get('status')}")
    for row in payload.get("agents") or []:
        if row.get("valid"):
            flags = "" if row.get("enabled") else " (disabled)"
            pre = row.get("preflight")
            pre_text = f" preflight={pre.get('status')}" if isinstance(pre, dict) else ""
            lines.append(
                f"- {row['id']}: {row['role']} {row['runtime']} provider={row['provider']} "
                f"model={row.get('model') or 'default'} x{row['max_concurrency']}{flags}{pre_text}"
            )
        else:
            lines.append(f"- {row['id']}: INVALID")
            lines.extend(f"    {issue}" for issue in row.get("issues") or [])
    if payload.get("agent_count") == 0:
        lines.append("no agents defined in: " + ", ".join(payload.get("search_paths") or []))
    return "\n".join(lines)


def render_provider_config(payload: dict[str, Any]) -> str:
    lines = [f"loopx {payload.get('command')}: {'ok' if payload.get('ok') else 'NOT OK'}"]
    for issue in payload.get("issues") or []:
        lines.append(f"- error: {issue}")
    for provider in payload.get("providers") or []:
        auth = provider.get("auth") or {}
        ref = auth.get("env") or (auth.get("keychain") or {}).get("service") or auth.get("cli") or ""
        lines.append(
            f"- {provider['name']}: {provider['kind']} auth={auth.get('type')}"
            + (f" ({ref})" if ref else "")
            + (f" base_url={provider['base_url']}" if provider.get("base_url") else "")
        )
    for check in payload.get("checks") or []:
        details = ", ".join(
            f"{item.get('kind')}:{item.get('env') or item.get('service') or item.get('cli') or ''}"
            f"={item.get('status')}"
            for item in check.get("checks") or []
        )
        lines.append(f"- {check['provider']}: {check['status']} [{details}]")
    if payload.get("provider_count") == 0:
        lines.append(f"no providers defined in {payload.get('providers_file')}")
    return "\n".join(lines)
