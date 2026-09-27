"""Record a run-once Turn's journaled ``turn_usage`` in the goal's usage ledger (G9).

Runs after ``run_loopx_turn_once`` returns, from the execution payload, so a
replayed or resumed Turn carries the same journaled usage and the ledger's
``<turn_key>#<attempt>`` key makes the write a no-op. Recording is fail-open:
accounting must never fail or change a Turn.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _provider_and_model(
    args: argparse.Namespace,
    *,
    agent_id: str,
    role: str | None,
    project: Path,
    runtime_root: Path,
) -> tuple[str | None, str | None]:
    host = getattr(args, "host", None)
    provider = getattr(args, "claude_provider" if host == "claude-code" else "codex_provider", None)
    model = getattr(args, "claude_model" if host == "claude-code" else "codex_model", None)
    if provider and model:
        return provider, model
    try:
        from ..agent_config import resolve_agent

        definition = resolve_agent(agent_id, project, runtime_root=runtime_root, role=role)
    except Exception:  # noqa: BLE001 - an agent without a config file is still accounted
        return provider, model
    return provider or definition.provider, model or definition.model


def _pricing(runtime_root: Path, provider: str | None) -> Any:
    if not provider:
        return None
    try:
        from ..agent_config import load_providers

        found = load_providers(runtime_root).get(provider)
    except Exception:  # noqa: BLE001 - a broken providers.yaml leaves the cost unpriced
        return None
    return found.pricing if found is not None else None


def record_turn_payload_usage(
    payload: Mapping[str, Any],
    *,
    args: argparse.Namespace,
    registry_path: Path,
    runtime_root: Path,
    project: Path,
    selected_todo: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    usage = payload.get("turn_usage")
    if not isinstance(usage, Mapping):
        return None
    try:
        from ..agent_registry import agent_roles_for_goal, load_goal_from_registry
        from ..usage_accounting import record_turn_usage

        goal_id = str(args.goal_id)
        agent_id = str(args.agent_id)
        role = agent_roles_for_goal(load_goal_from_registry(registry_path, goal_id)).get(agent_id)
        provider, model = _provider_and_model(
            args, agent_id=agent_id, role=role, project=project, runtime_root=runtime_root,
        )
        failure = payload.get("host_failure")
        return record_turn_usage(
            runtime_root,
            goal_id,
            usage,
            agent_id=agent_id,
            turn_key=str(payload.get("resume_turn_key") or ""),
            role=role,
            todo_id=str((selected_todo or {}).get("todo_id") or "") or None,
            provider=provider,
            model=model,
            outcome=payload.get("result_kind") if isinstance(payload.get("result_kind"), str) else None,
            status=payload.get("status") if isinstance(payload.get("status"), str) else None,
            failure_kind=failure.get("kind") if isinstance(failure, Mapping) else None,
            pricing=_pricing(runtime_root, provider),
        )
    except Exception as exc:  # noqa: BLE001 - accounting never fails a Turn
        return {"recorded": False, "reason": type(exc).__name__}
