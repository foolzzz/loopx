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
* Changing criteria is a major change (decision 12). A CLI change is recorded
  in the rollout event log with the old and new digests; it is not gated by a
  plan card.

The orchestrator edits todos that a developer has claimed. As with the
acceptor's verdicts, the lifecycle write is attributed to the claim owner, so
the kernel's claim fence and task leases are unchanged; the orchestrator's
authority is checked here and recorded in the returned packet.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .control_plane.todos.contract import (
    normalize_todo_acceptance_criteria,
    normalize_todo_claimed_by,
)

ACCEPTANCE_CRITERIA_CHANGE_SCHEMA_VERSION = "loopx_todo_acceptance_criteria_change_v0"
# Decision 12: a change to acceptance criteria is a major plan change.
ACCEPTANCE_CRITERIA_CHANGE_CLASS = "major"


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
        f"under role_v1 only the goal orchestrator writes a todo's acceptance_criteria; "
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
    """Write (or clear, with ``None``) one todo's acceptance criteria."""

    from .todo_acceptance import _read_todo
    from .todos import update_goal_todo

    require_acceptance_criteria_author(
        registry_path=registry_path, goal_id=goal_id, actor_agent_id=agent_id,
    )
    author = normalize_todo_claimed_by(agent_id)
    todo = _read_todo(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id,
        runtime_root_arg=runtime_root_arg, project=project, state_file=state_file,
    )
    if todo is None:
        raise ValueError(f"todo_id not found: {todo_id}")
    owner = normalize_todo_claimed_by(todo.get("claimed_by"))
    lifecycle_actor = owner if author and owner else author
    normalized = normalize_todo_acceptance_criteria(acceptance_criteria)
    result = update_goal_todo(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id,
        runtime_root_arg=runtime_root_arg, project=project, state_file=state_file,
        agent_id=lifecycle_actor, acceptance_criteria_author=author,
        role_contract={"acceptance_criteria": normalized}, dry_run=dry_run,
    )
    previous = acceptance_criteria_sha256(todo.get("acceptance_criteria"))
    current = acceptance_criteria_sha256(normalized)
    result["acceptance_criteria_change"] = {
        "schema_version": ACCEPTANCE_CRITERIA_CHANGE_SCHEMA_VERSION,
        "change_class": ACCEPTANCE_CRITERIA_CHANGE_CLASS,
        "author": author,
        "lifecycle_actor": lifecycle_actor,
        "changed": previous != current,
        "previous_sha256": previous,
        "sha256": current,
    }
    return result


def acceptance_criteria_event_details(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Flat, public-safe rollout details for a criteria change (no criteria text)."""

    change = payload.get("acceptance_criteria_change")
    if not isinstance(change, Mapping):
        return {}
    return {
        "acceptance_criteria_changed": bool(change.get("changed")),
        "acceptance_criteria_change_class": change.get("change_class"),
        "acceptance_criteria_author": change.get("author"),
        "acceptance_criteria_previous_sha256": change.get("previous_sha256"),
        "acceptance_criteria_sha256": change.get("sha256"),
    }
