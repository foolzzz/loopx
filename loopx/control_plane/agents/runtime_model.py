from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Iterable, Mapping

from ..todos.contract import normalize_todo_claimed_by


PEER_AGENT_IDENTITY_SCHEMA_VERSION = "peer_agent_identity_v1"
PEER_AGENT_PROFILE_SCHEMA_VERSION = "agent_profile_v1"
LEGACY_AGENT_PROFILE_SCHEMA_VERSION = "agent_profile_v0"


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


class RetiredAgentHierarchyError(ValueError):
    """Public-safe rejection for retired v0.1 main/side goal state."""

    error_code = "retired_agent_hierarchy"

    def __init__(self, fields: Iterable[str]) -> None:
        self.fields = tuple(fields)
        self.recommended_action = (
            "remove the listed fields from the source registry, set "
            "coordination.agent_model=role_v1 or peer_v1, keep the current "
            "coordination.registered_agents roster, and rerun the command; "
            "quota should-run must use a registered --agent-id"
        )
        super().__init__(
            "goal contains retired v0.1 agent hierarchy fields: "
            + ", ".join(self.fields)
            + "; "
            + self.recommended_action
            + ". LoopX no longer migrates main/side hierarchy state."
        )


def normalize_agent_role(value: Any) -> str | None:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in AGENT_ROLE_VALUES else None


def _profile_mapping_path(agent_id: Any) -> str:
    raw_agent_id = str(agent_id)
    if normalize_todo_claimed_by(raw_agent_id) == raw_agent_id:
        return f"coordination.agent_profiles[{json.dumps(raw_agent_id)}]"
    fingerprint = hashlib.sha256(raw_agent_id.encode("utf-8")).hexdigest()[:12]
    return f'coordination.agent_profiles["<invalid-agent-id:{fingerprint}>"]'


def legacy_agent_hierarchy_fields(
    goal: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Return retired v0.1 main/side fields still present in a goal."""

    if not isinstance(goal, Mapping):
        return ()
    coordination = goal.get("coordination")
    if not isinstance(coordination, Mapping):
        coordination = {}
    fields: list[str] = []
    configured_model = coordination.get("agent_model")
    if configured_model == "legacy_hierarchy":
        fields.append("coordination.agent_model")
    if goal.get("agent_model") == "legacy_hierarchy":
        fields.append("agent_model")
    for field in ("primary_agent", "side_agent_handoff_agent"):
        if field in coordination:
            fields.append(f"coordination.{field}")

    profiles = coordination.get("agent_profiles")
    if isinstance(profiles, Mapping):
        profile_items = (
            (
                _profile_mapping_path(agent_id),
                profile,
            )
            for agent_id, profile in profiles.items()
        )
    elif isinstance(profiles, list):
        profile_items = (
            (
                f"coordination.agent_profiles[{index}]",
                profile,
            )
            for index, profile in enumerate(profiles)
            if isinstance(profile, Mapping)
        )
    else:
        profile_items = ()
    effective_model = configured_model or goal.get("agent_model")
    role_v1 = effective_model in {None, "", AgentRuntimeModel.ROLE_V1.value}
    for prefix, profile in profile_items:
        if not isinstance(profile, Mapping):
            continue
        if profile.get("schema_version") == LEGACY_AGENT_PROFILE_SCHEMA_VERSION:
            fields.append(f"{prefix}.schema_version")
        if not role_v1 and "role" in profile:
            fields.append(f"{prefix}.role")
        if "primary_agent" in profile:
            fields.append(f"{prefix}.primary_agent")
        if "worktree_policy" in profile:
            fields.append(f"{prefix}.worktree_policy")
        review_policy = profile.get("review_policy")
        if "review_policy" in profile:
            matched_review_policy_field = False
            for field in (
                "handoff_agent",
                "reviews_side_agent_work",
                "can_self_merge",
            ):
                if not isinstance(review_policy, Mapping):
                    break
                if field in review_policy:
                    fields.append(f"{prefix}.review_policy.{field}")
                    matched_review_policy_field = True
            if not matched_review_policy_field:
                fields.append(f"{prefix}.review_policy")
    completed_migrations = coordination.get("completed_migrations")
    if (
        isinstance(completed_migrations, Mapping)
        and "peer_agent_runtime_v1" in completed_migrations
    ):
        fields.append("coordination.completed_migrations.peer_agent_runtime_v1")
    return tuple(fields)


def reject_legacy_agent_hierarchy(goal: Mapping[str, Any] | None) -> None:
    fields = legacy_agent_hierarchy_fields(goal)
    if not fields:
        return
    raise RetiredAgentHierarchyError(fields)


def agent_runtime_model_for_goal(goal: Mapping[str, Any] | None) -> AgentRuntimeModel:
    """Return the goal's agent runtime model (role_v1 unless peer_v1 is recorded)."""

    reject_legacy_agent_hierarchy(goal)
    if isinstance(goal, Mapping):
        coordination = goal.get("coordination")
        raw = coordination.get("agent_model") if isinstance(coordination, Mapping) else None
        raw = raw or goal.get("agent_model")
        if raw == AgentRuntimeModel.ROLE_V1.value:
            return AgentRuntimeModel.ROLE_V1
        if raw == AgentRuntimeModel.PEER_V1.value:
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
    except RetiredAgentHierarchyError:
        raise
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


def next_action_executable_demands_agent_todo(
    agent_runtime_model: AgentRuntimeModel | None,
) -> bool:
    """Whether an executable Next Action demands an Agent Todo (fork decision 42).

    peer_v1 agents carry their own next step in the goal's Next Action, so an
    executable Next Action with no open Agent Todo is a state-projection gap
    to repair. Under role_v1 the Next Action is written by whichever Turn
    settled last (typically the acceptor: "Settle todo_X as accepted"), so it
    is no orchestrator obligation; once no agent todo is open the goal's work
    is finished, and the dispatcher's push and goal_complete gates take over.
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
