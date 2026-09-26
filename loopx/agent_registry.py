from __future__ import annotations

from pathlib import Path
from typing import Any

from .control_plane.todos.contract import normalize_todo_claimed_by


def normalize_registered_agents(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]
    agents: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("id") or value.get("agent_id") or value.get("name")
        agent = normalize_todo_claimed_by(value)
        if agent and agent not in agents:
            agents.append(agent)
    return agents


def registered_agent_ids_for_goal(goal: dict[str, Any] | None) -> list[str]:
    if not isinstance(goal, dict):
        return []
    candidates: list[Any] = []
    coordination = goal.get("coordination")
    if isinstance(coordination, dict):
        candidates.append(coordination.get("registered_agents"))
    candidates.append(goal.get("registered_agents"))
    spawn_policy = goal.get("spawn_policy")
    if isinstance(spawn_policy, dict):
        candidates.append(spawn_policy.get("registered_agents"))
    agents: list[str] = []
    for candidate in candidates:
        for agent in normalize_registered_agents(candidate):
            if agent not in agents:
                agents.append(agent)
    return agents


def normalize_agent_roles(
    values: Any,
    *,
    registered_agents: list[str] | None = None,
) -> dict[str, str]:
    """Normalize ``coordination.agent_roles`` ({agent_id: role}).

    Unknown roles and agents outside ``registered_agents`` (when given) are
    rejected so the registry never stores an ambiguous role map. At most one
    orchestrator is allowed per goal.
    """

    from .control_plane.agents.runtime_model import (
        AGENT_ROLE_ORCHESTRATOR,
        AGENT_ROLE_VALUES,
        normalize_agent_role,
    )

    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ValueError("coordination.agent_roles must map agent ids to roles")
    roles: dict[str, str] = {}
    for raw_agent, raw_role in values.items():
        agent = normalize_todo_claimed_by(raw_agent)
        if not agent:
            raise ValueError(
                "agent role keys must be public-safe agent ids such as codex-dev-1"
            )
        role = normalize_agent_role(raw_role)
        if not role:
            raise ValueError(
                f"agent role for {agent!r} must be one of: " + ", ".join(AGENT_ROLE_VALUES)
            )
        if registered_agents is not None and agent not in registered_agents:
            raise ValueError(
                f"agent role for {agent!r} requires a registered agent; "
                f"registered_agents={', '.join(registered_agents) or '(none)'}"
            )
        roles[agent] = role
    orchestrators = sorted(
        agent for agent, role in roles.items() if role == AGENT_ROLE_ORCHESTRATOR
    )
    if len(orchestrators) > 1:
        raise ValueError(
            "a goal may have at most one orchestrator; found: " + ", ".join(orchestrators)
        )
    return dict(sorted(roles.items()))


def agent_roles_for_goal(goal: dict[str, Any] | None) -> dict[str, str]:
    """Return the goal's registered agent roles, ignoring malformed entries."""

    if not isinstance(goal, dict):
        return {}
    coordination = goal.get("coordination")
    raw = coordination.get("agent_roles") if isinstance(coordination, dict) else None
    if not isinstance(raw, dict):
        return {}
    registered = registered_agent_ids_for_goal(goal)
    from .control_plane.agents.runtime_model import normalize_agent_role

    roles: dict[str, str] = {}
    for raw_agent, raw_role in raw.items():
        agent = normalize_todo_claimed_by(raw_agent)
        role = normalize_agent_role(raw_role)
        if agent and role and agent in registered:
            roles[agent] = role
    return dict(sorted(roles.items()))


def agent_role_for_goal(goal: dict[str, Any] | None, agent_id: str | None) -> str | None:
    agent = normalize_todo_claimed_by(agent_id)
    if not agent:
        return None
    return agent_roles_for_goal(goal).get(agent)


def orchestrator_agent_for_goal(goal: dict[str, Any] | None) -> str | None:
    """Return the goal's single orchestrator under role_v1, else None."""

    from .control_plane.agents.runtime_model import (
        AGENT_ROLE_ORCHESTRATOR,
        AgentRuntimeModel,
        agent_runtime_model_for_goal,
    )

    try:
        if agent_runtime_model_for_goal(goal) != AgentRuntimeModel.ROLE_V1:
            return None
    except ValueError:
        return None
    orchestrators = sorted(
        agent
        for agent, role in agent_roles_for_goal(goal).items()
        if role == AGENT_ROLE_ORCHESTRATOR
    )
    return orchestrators[0] if len(orchestrators) == 1 else None


def agent_profile_for_goal(goal: dict[str, Any] | None, agent_id: str | None) -> dict[str, Any] | None:
    normalized_agent_id = normalize_todo_claimed_by(agent_id)
    if not isinstance(goal, dict) or not normalized_agent_id:
        return None
    coordination = goal.get("coordination")
    if not isinstance(coordination, dict):
        return None
    profiles = coordination.get("agent_profiles")
    raw_profile: Any = None
    if isinstance(profiles, dict):
        raw_profile = profiles.get(normalized_agent_id)
    elif isinstance(profiles, list):
        for item in profiles:
            if not isinstance(item, dict):
                continue
            item_id = normalize_todo_claimed_by(item.get("agent_id") or item.get("id") or item.get("name"))
            if item_id == normalized_agent_id:
                raw_profile = item
                break
    if not isinstance(raw_profile, dict):
        return None
    profile = dict(raw_profile)
    profile["agent_id"] = normalized_agent_id
    return profile


def load_goal_from_registry(registry_path: Path, goal_id: str) -> dict[str, Any] | None:
    from .history import load_registry
    from .registry import registry_goals

    if not registry_path.exists():
        return None
    registry = load_registry(registry_path)
    return next(
        (goal for goal in registry_goals(registry) if str(goal.get("id")) == str(goal_id)),
        None,
    )


def registered_agent_ids_from_registry(registry_path: Path, goal_id: str) -> list[str]:
    return registered_agent_ids_for_goal(load_goal_from_registry(registry_path, goal_id))


def agent_profile_from_registry(registry_path: Path, goal_id: str, agent_id: str | None) -> dict[str, Any] | None:
    return agent_profile_for_goal(load_goal_from_registry(registry_path, goal_id), agent_id)


def require_registered_agent_id(
    *,
    registry_path: Path,
    goal_id: str,
    agent_id: str | None,
    field: str = "claimed_by",
) -> str:
    normalized = normalize_todo_claimed_by(agent_id)
    if not normalized:
        raise ValueError(f"{field} must be a public-safe registered agent id")
    registered = registered_agent_ids_from_registry(registry_path, goal_id)
    if not registered:
        raise ValueError(
            f"{field}={normalized!r} cannot be used because goal {goal_id!r} "
            "has no coordination.registered_agents list. Register this peer identity first: "
            "loopx configure-goal --goal-id "
            f"{goal_id} --registered-agent {normalized} --execute"
        )
    if normalized not in registered:
        raise ValueError(
            f"{field}={normalized!r} is not registered for goal {goal_id!r}; "
            f"registered_agents={', '.join(registered)}"
        )
    return normalized
