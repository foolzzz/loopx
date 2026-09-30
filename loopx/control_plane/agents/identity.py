from __future__ import annotations

from typing import Any

from ...agent_registry import (
    acceptor_agents_for_goal,
    agent_profile_for_goal,
    agent_role_for_goal,
    orchestrator_agent_for_goal,
    registered_agent_ids_for_goal,
)
from ..quota.error_codes import (
    QuotaIdentityPrecondition,
    QuotaIdentityPreconditionError,
)
from ..todos.contract import normalize_todo_claimed_by
from .profile import normalize_agent_profile
from .runtime_model import (
    PEER_AGENT_IDENTITY_SCHEMA_VERSION,
    AgentRuntimeModel,
    agent_runtime_model_for_goal,
    reject_legacy_agent_hierarchy,
)
from .work_mode import agent_work_mode_for_goal


def quota_registered_agents(goal: dict[str, Any]) -> list[str]:
    return registered_agent_ids_for_goal(goal)


def build_quota_agent_identity(
    goal: dict[str, Any],
    *,
    agent_id: str | None,
) -> dict[str, Any] | None:
    normalized_agent_id = normalize_todo_claimed_by(agent_id) if agent_id else None
    if agent_id and not normalized_agent_id:
        raise QuotaIdentityPreconditionError(
            QuotaIdentityPrecondition.PUBLIC_SAFE_AGENT_ID
        )
    registered_agents = quota_registered_agents(goal)
    if not normalized_agent_id:
        return None
    if not registered_agents:
        raise QuotaIdentityPreconditionError(
            QuotaIdentityPrecondition.REGISTERED_AGENT_ROSTER_PRESENT,
            agent_id=normalized_agent_id,
        )
    if normalized_agent_id not in registered_agents:
        raise QuotaIdentityPreconditionError(
            QuotaIdentityPrecondition.REQUESTED_AGENT_REGISTERED,
            agent_id=normalized_agent_id,
        )
    runtime_model = agent_runtime_model_for_goal(goal)
    identity = {
        "schema_version": PEER_AGENT_IDENTITY_SCHEMA_VERSION,
        "agent_model": runtime_model.value,
        "agent_id": normalized_agent_id,
        "registered": True,
        "registered_agents": registered_agents,
    }
    if runtime_model == AgentRuntimeModel.ROLE_V1:
        role = agent_role_for_goal(goal, normalized_agent_id)
        if role:
            identity["role"] = role
        orchestrator = orchestrator_agent_for_goal(goal)
        if orchestrator:
            identity["orchestrator_agent_id"] = orchestrator
        acceptors = acceptor_agents_for_goal(goal)
        if acceptors:
            identity["acceptor_agent_ids"] = acceptors
    work_mode = agent_work_mode_for_goal(goal, normalized_agent_id)
    if work_mode:
        identity["work_mode"] = work_mode
    raw_profile = agent_profile_for_goal(goal, normalized_agent_id)
    if raw_profile:
        try:
            identity["agent_profile"] = normalize_agent_profile(
                raw_profile,
                registered_agents=registered_agents,
                expected_agent_id=normalized_agent_id,
                reject_unknown_fields=False,
            )
        except ValueError:
            # Advisory metadata must not block an otherwise valid peer identity.
            pass
    return identity


def build_identity_aware_prompt_upgrade(
    goal: dict[str, Any],
    *,
    goal_id: str,
    agent_identity: dict[str, Any] | None,
) -> dict[str, Any] | None:
    reject_legacy_agent_hierarchy(goal)
    registered_agents = quota_registered_agents(goal)
    if not registered_agents:
        return None
    if agent_identity:
        return None
    runtime_model = agent_runtime_model_for_goal(goal)
    return {
        "contract": "peer_agent_heartbeat_prompt_v1",
        "required": True,
        "blocks_should_run": True,
        "reason": (
            "coordination.registered_agents is configured, but quota should-run was "
            "called without --agent-id; the installed automation prompt is stale or "
            "unscoped"
        ),
        "agent_model": runtime_model.value,
        "registered_agents": registered_agents,
        "recommended_action": (
            "Regenerate each installed heartbeat with its registered --agent-id, "
            "then rerun quota should-run with the same identity."
        ),
        "agent_example_commands": [
            {
                "agent_id": agent,
                "command": (
                    f"loopx heartbeat-prompt --thin --goal-id {goal_id} "
                    f"--agent-id {agent} --agent-scope 'peer task claims and leases'"
                ),
            }
            for agent in registered_agents
        ],
    }
