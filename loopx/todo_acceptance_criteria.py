"""Orchestrator-owned per-todo acceptance criteria (fork gap G2).

Design decision 9: per-todo criteria are written by the orchestrator, and the
acceptor checks them together with the goal-level acceptance contract. The
criteria live in the dedicated ``acceptance_criteria`` todo field, never in
the mutable ``note`` that a Turn completion overwrites.

* Under role_v1 only the goal orchestrator (or the owner, with no agent id)
  may write the field. Developers and acceptors are refused. Agents without a
  registered role and ``peer_v1`` goals keep the previous behaviour.
* Delivery, verdicts and Turn writeback never write it: their role-contract
  patches name only their own fields.
* Changing criteria is a major change (decision 12). Under role_v1 (with a
  registered orchestrator) no agent changes an existing todo's criteria
  directly: the change takes effect only through a user-approved plan card
  with a ``criteria_changes`` entry (decision 40, ``loopx.plan_criteria_changes``).
  The owner (no agent id) may still edit directly; the edit is recorded in the
  rollout event log with the old and new digests.

An edit of a todo that a developer has claimed is attributed, as a lifecycle
write, to the claim owner (else the goal orchestrator), so the kernel's claim
fence and task leases are unchanged; the real author is checked here and
recorded in the returned packet.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .control_plane.todos.contract import (
    normalize_todo_acceptance_criteria,
    normalize_todo_claimed_by,
)

ACCEPTANCE_CRITERIA_CHANGE_SCHEMA_VERSION = "loopx_todo_acceptance_criteria_change_v0"
# Decision 12: a change to acceptance criteria is a major plan change.
ACCEPTANCE_CRITERIA_CHANGE_CLASS = "major"
# Decision 40: an agent's change to an existing todo's criteria needs a plan card.
ACCEPTANCE_CRITERIA_CHANGE_REQUIRES_PLAN = "acceptance_criteria_change_requires_plan"
ACCEPTANCE_CRITERIA_SOURCE_OWNER = "owner"
ACCEPTANCE_CRITERIA_SOURCE_AGENT = "agent"
ACCEPTANCE_CRITERIA_SOURCE_PLAN = "plan_card"

# Set only while an owner edit or an approved plan card writes the field; the
# update guard lets exactly those writes through under role_v1.
_AUTHORIZED_CRITERIA_WRITE: ContextVar[str | None] = ContextVar("loopx_authorized_criteria_write", default=None)


class AcceptanceCriteriaAuthorError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def acceptance_criteria_sha256(value: Any) -> str | None:
    text = normalize_todo_acceptance_criteria(value)
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _goal(registry_path: Path, goal_id: str) -> dict[str, Any] | None:
    from .agent_registry import load_goal_from_registry

    return load_goal_from_registry(Path(registry_path), goal_id)


def require_acceptance_criteria_author(
    *, registry_path: Path, goal_id: str, actor_agent_id: str | None,
) -> None:
    """Refuse acceptance-criteria writes by developer or acceptor agents (role_v1)."""

    actor = normalize_todo_claimed_by(actor_agent_id)
    if not actor:
        return
    from .agent_registry import agent_role_for_goal
    from .control_plane.agents.runtime_model import AGENT_ROLE_ORCHESTRATOR
    from .todo_acceptance import goal_uses_role_v1

    goal = _goal(registry_path, goal_id)
    if goal is None or not goal_uses_role_v1(goal):
        return
    role = agent_role_for_goal(goal, actor)
    if role is None or role == AGENT_ROLE_ORCHESTRATOR:
        return
    raise AcceptanceCriteriaAuthorError(
        "acceptance_criteria_requires_orchestrator",
        f"under role_v1 only the goal orchestrator writes a todo's acceptance_criteria "
        f"(on an existing todo through a user-approved plan card); "
        f"{role} agent {actor!r} cannot. Raise a blocker to the orchestrator instead: "
        f"loopx todo add --goal-id {goal_id} --role agent --task-class blocker "
        "--text '<the criteria question>'",
    )


def guard_acceptance_criteria_write(
    registry_path: Path, goal_id: str, actor_agent_id: str | None, patch: Mapping[str, Any] | None,
) -> None:
    """Todo add/update hook: check the author when a patch writes the field.

    ``set_goal_todo_acceptance_criteria`` passes the orchestrator as the
    author while it attributes the lifecycle write to the claim owner.
    """

    if patch and "acceptance_criteria" in patch:
        require_acceptance_criteria_author(
            registry_path=registry_path, goal_id=goal_id, actor_agent_id=actor_agent_id,
        )


@contextmanager
def authorized_criteria_write(source: str) -> Iterator[None]:
    token = _AUTHORIZED_CRITERIA_WRITE.set(source)
    try:
        yield
    finally:
        _AUTHORIZED_CRITERIA_WRITE.reset(token)


def criteria_changes_need_plan_card(goal: Mapping[str, Any] | None) -> bool:
    """Decision 40 applies to role_v1 goals that have an orchestrator to propose."""

    from .agent_registry import orchestrator_agent_for_goal
    from .todo_acceptance import goal_uses_role_v1

    return bool(goal) and goal_uses_role_v1(goal) and orchestrator_agent_for_goal(dict(goal or {})) is not None


def require_acceptance_criteria_change_authority(
    *, registry_path: Path, goal_id: str, actor_agent_id: str | None,
) -> None:
    """Refuse an agent's direct change to an existing todo's criteria (decision 40).

    The owner (no agent id) and an approved plan card may change them. Goals
    that are not role_v1, or have no orchestrator, keep the G2 author rule.
    """

    actor = normalize_todo_claimed_by(actor_agent_id)
    if not actor or _AUTHORIZED_CRITERIA_WRITE.get():
        return
    require_acceptance_criteria_author(
        registry_path=registry_path, goal_id=goal_id, actor_agent_id=actor,
    )
    goal = _goal(registry_path, goal_id)
    if not criteria_changes_need_plan_card(goal):
        return
    from .agent_registry import agent_role_for_goal, orchestrator_agent_for_goal

    role = agent_role_for_goal(goal, actor) or "agent"
    orchestrator = orchestrator_agent_for_goal(dict(goal or {}))
    route = (
        f"Propose it: loopx plan propose --goal-id {goal_id} --agent-id {orchestrator} --plan-file plan.json "
        "with {\"title\": \"...\", \"criteria_changes\": [{\"todo_id\": \"<todo>\", \"new\": \"<criteria>\", "
        "\"reason\": \"<why>\"}]}; the acceptor does not review that todo until the user decides."
        if actor == orchestrator else
        f"Raise a blocker to the orchestrator instead: loopx todo add --goal-id {goal_id} --role agent "
        "--task-class blocker --text '<the criteria question>'"
    )
    raise AcceptanceCriteriaAuthorError(
        ACCEPTANCE_CRITERIA_CHANGE_REQUIRES_PLAN,
        f"under role_v1 a change to an existing todo's acceptance_criteria takes effect only through "
        f"a user-approved plan card (design decision 40); {role} {actor!r} cannot change it directly. {route}",
    )


def guard_acceptance_criteria_update(
    registry_path: Path, goal_id: str, actor_agent_id: str | None, patch: Mapping[str, Any] | None,
) -> None:
    """Todo update hook: an existing todo's criteria change needs a plan card (role_v1)."""

    if patch and "acceptance_criteria" in patch:
        require_acceptance_criteria_change_authority(
            registry_path=registry_path, goal_id=goal_id, actor_agent_id=actor_agent_id,
        )


def set_goal_todo_acceptance_criteria(
    *,
    registry_path: Path,
    goal_id: str,
    todo_id: str,
    acceptance_criteria: str | None,
    agent_id: str | None = None,
    runtime_root_arg: str | None = None,
    project: Path | None = None,
    state_file: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write (or clear, with ``None``) one todo's acceptance criteria directly.

    Under role_v1 only the owner (no agent id) may; agents propose a plan card.
    """

    require_acceptance_criteria_change_authority(
        registry_path=registry_path, goal_id=goal_id, actor_agent_id=agent_id,
    )
    return write_goal_todo_acceptance_criteria(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id,
        acceptance_criteria=acceptance_criteria, author=agent_id,
        source=ACCEPTANCE_CRITERIA_SOURCE_AGENT if normalize_todo_claimed_by(agent_id) else ACCEPTANCE_CRITERIA_SOURCE_OWNER,
        runtime_root_arg=runtime_root_arg, project=project, state_file=state_file, dry_run=dry_run,
    )


class StaleAcceptanceCriteriaError(ValueError):
    """The todo's criteria changed after a plan card captured them."""


def write_goal_todo_acceptance_criteria(
    *,
    registry_path: Path,
    goal_id: str,
    todo_id: str,
    acceptance_criteria: str | None,
    author: str | None,
    source: str,
    plan_id: str | None = None,
    expected_previous: str | None = None,
    check_previous: bool = False,
    runtime_root_arg: str | None = None,
    project: Path | None = None,
    state_file: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write the field after the caller checked the authority.

    ``check_previous`` refuses the write (``StaleAcceptanceCriteriaError``)
    when the stored criteria no longer equal ``expected_previous``.
    """

    from .agent_registry import orchestrator_agent_for_goal
    from .todo_acceptance import _read_todo
    from .todos import update_goal_todo

    author = normalize_todo_claimed_by(author)
    todo = _read_todo(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id,
        runtime_root_arg=runtime_root_arg, project=project, state_file=state_file,
    )
    if todo is None:
        raise ValueError(f"todo_id not found: {todo_id}")
    previous_text = normalize_todo_acceptance_criteria(todo.get("acceptance_criteria"))
    if check_previous and previous_text != normalize_todo_acceptance_criteria(expected_previous):
        raise StaleAcceptanceCriteriaError(
            f"todo {todo_id!r} acceptance_criteria changed after the proposal; the change is stale"
        )
    owner = normalize_todo_claimed_by(todo.get("claimed_by"))
    if author:
        lifecycle_actor = owner or author
    else:
        # An owner or plan-card write is attributed to the claim owner (else the
        # orchestrator), as a gate decision is; multi-agent goals need an actor.
        goal = _goal(registry_path, goal_id)
        lifecycle_actor = owner or (orchestrator_agent_for_goal(dict(goal)) if goal else None)
    normalized = normalize_todo_acceptance_criteria(acceptance_criteria)
    authorized = source in {ACCEPTANCE_CRITERIA_SOURCE_OWNER, ACCEPTANCE_CRITERIA_SOURCE_PLAN}
    with authorized_criteria_write(source) if authorized else _unauthorized():
        result = update_goal_todo(
            registry_path=registry_path, goal_id=goal_id, todo_id=todo_id,
            runtime_root_arg=runtime_root_arg, project=project, state_file=state_file,
            agent_id=lifecycle_actor, acceptance_criteria_author=author,
            role_contract={"acceptance_criteria": normalized}, dry_run=dry_run,
        )
    previous = acceptance_criteria_sha256(previous_text)
    current = acceptance_criteria_sha256(normalized)
    result["acceptance_criteria_change"] = {
        "schema_version": ACCEPTANCE_CRITERIA_CHANGE_SCHEMA_VERSION,
        "change_class": ACCEPTANCE_CRITERIA_CHANGE_CLASS,
        "author": author,
        "source": source,
        **({"plan_id": plan_id} if plan_id else {}),
        "lifecycle_actor": lifecycle_actor,
        "changed": previous != current,
        "previous_sha256": previous,
        "sha256": current,
    }
    return result


@contextmanager
def _unauthorized() -> Iterator[None]:
    yield


def acceptance_criteria_event_details(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Flat, public-safe rollout details for a criteria change (no criteria text)."""

    change = payload.get("acceptance_criteria_change")
    if not isinstance(change, Mapping):
        return {}
    return {
        "acceptance_criteria_changed": bool(change.get("changed")),
        "acceptance_criteria_change_class": change.get("change_class"),
        "acceptance_criteria_author": change.get("author"),
        "acceptance_criteria_source": change.get("source"),
        "acceptance_criteria_previous_sha256": change.get("previous_sha256"),
        "acceptance_criteria_sha256": change.get("sha256"),
    }
