from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Iterable, Mapping

from ..todos.contract import normalize_todo_claimed_by


PEER_AGENT_IDENTITY_SCHEMA_VERSION = "peer_agent_identity_v1"
PEER_AGENT_PROFILE_SCHEMA_VERSION = "agent_profile_v1"


class AgentRuntimeModel(str, Enum):
    ROLE_V1 = "role_v1"
    PEER_V1 = "peer_v1"


# New goals (no configured model) use role_v1; peer_v1 remains readable for
# goals that already recorded it.
DEFAULT_AGENT_RUNTIME_MODEL = AgentRuntimeModel.ROLE_V1

AGENT_ROLE_ORCHESTRATOR = "orchestrator"
AGENT_ROLE_DEVELOPER = "developer"
AGENT_ROLE_ACCEPTOR = "acceptor"
AGENT_ROLE_VALUES = (
    AGENT_ROLE_ORCHESTRATOR,
    AGENT_ROLE_DEVELOPER,
    AGENT_ROLE_ACCEPTOR,
)


def normalize_agent_role(value: Any) -> str | None:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in AGENT_ROLE_VALUES else None


def agent_runtime_model_for_goal(goal: Mapping[str, Any] | None) -> AgentRuntimeModel:
    """Return the goal's agent runtime model (role_v1 unless peer_v1 is recorded)."""

    if isinstance(goal, Mapping):
        coordination = goal.get("coordination")
        raw = coordination.get("agent_model") if isinstance(coordination, Mapping) else None
        raw = raw or goal.get("agent_model")
        if raw == AgentRuntimeModel.ROLE_V1.value:
            return AgentRuntimeModel.ROLE_V1
        if raw in {AgentRuntimeModel.PEER_V1.value, "legacy_hierarchy"}:
            return AgentRuntimeModel.PEER_V1
        if raw not in {None, ""}:
            raise ValueError("coordination.agent_model must be role_v1 or peer_v1")
    return DEFAULT_AGENT_RUNTIME_MODEL


def goal_agent_runtime_model_or_none(
    goal: Mapping[str, Any] | None,
) -> AgentRuntimeModel | None:
    """Return the goal's runtime model, or None without a goal or with an invalid model."""

    if not isinstance(goal, Mapping):
        return None
    try:
        return agent_runtime_model_for_goal(goal)
    except ValueError:
        return None


def orchestrator_owns_planning_review(
    agent_runtime_model: AgentRuntimeModel | None,
) -> bool:
    """Whether planning review belongs to the orchestrator (fork decision 31).

    Under role_v1 every orchestrator Turn reviews the full goal state, and
    developers and acceptors escalate through orchestrator todos. The upstream
    per-agent vision checkpoint and the no-follow-up succession replan are
    therefore not derived for role_v1 goals; peer_v1 goals keep them.
    """

    return agent_runtime_model is AgentRuntimeModel.ROLE_V1


def next_action_wait_demands_user_todo(
    agent_runtime_model: AgentRuntimeModel | None,
) -> bool:
    """Whether a Next Action that reads as a wait demands a User Todo (fork decision 39).

    peer_v1 agents self-report a wait in their Next Action, so a wait without
    an open User Todo is a state-projection gap to repair. Under role_v1 an
    idle orchestrator is the normal state: it is event-triggered, and a Next
    Action such as "re-engages only if ... a user gate appears" is not a wait
    on the user. Stuck work is detected by the dispatcher instead (gate
    replies, escalations, replan obligations from run history).
    """

    return agent_runtime_model is not AgentRuntimeModel.ROLE_V1


def agent_identity_is_peer(agent_identity: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(agent_identity, Mapping)
        and agent_identity.get("agent_model")
        in {AgentRuntimeModel.PEER_V1.value, AgentRuntimeModel.ROLE_V1.value}
    )


def normalized_peer_agent_ids(values: Iterable[Any]) -> list[str]:
    agents = sorted(
        {
            agent
            for value in values
            for agent in [
                normalize_todo_claimed_by(
                    value.get("id") or value.get("agent_id") or value.get("name")
                    if isinstance(value, Mapping)
                    else value
                )
            ]
            if agent
        }
    )
    return agents


def peer_work_key(value: Mapping[str, Any] | None, *, fallback: str) -> str:
    if not isinstance(value, Mapping):
        return fallback
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def select_peer_for_work(
    registered_agents: Iterable[Any],
    *,
    work_key: str,
) -> str | None:
    agents = normalized_peer_agent_ids(registered_agents)
    if not agents:
        return None
    digest = hashlib.sha256(str(work_key).encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % len(agents)
    return agents[index]
