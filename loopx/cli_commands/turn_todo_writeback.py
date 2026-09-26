"""Todo writeback adapters that preserve the Turn's effective runtime root."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..todos import complete_goal_todo, update_goal_todo


def write_turn_repair_update(
    *,
    registry_path: Path,
    runtime_root_arg: str | None,
    goal_id: str,
    todo_id: str,
    note: str,
    evidence: str,
    agent_id: str | None,
    result_kind: str | None = None,
) -> None:
    """Record one repair-required Todo note under the effective runtime root.

    ``runtime_root_arg`` is required on purpose: the Turn settlement must
    hand down the same ``--runtime-root`` override the dispatch resolved, so
    the legacy writer fence and the todo mutex of a promotion cannot split
    from the Turn writeback path.

    Under role_v1 an acceptor Turn that returns ``repair_required`` for the
    delivered (``in_review``) Todo it reviews records a reject verdict with
    the result summary as feedback (fork S2).
    """

    if result_kind == "repair_required" and agent_id:
        from ..todo_acceptance import reject_delivery_from_turn

        if reject_delivery_from_turn(
            registry_path=registry_path, runtime_root_arg=runtime_root_arg,
            goal_id=goal_id, todo_id=todo_id, agent_id=agent_id, feedback=note,
        ):
            return
    update_goal_todo(
        registry_path=registry_path,
        goal_id=goal_id,
        todo_id=todo_id,
        role="agent",
        note=note,
        evidence=evidence,
        agent_id=agent_id,
        project=None,
        dry_run=False,
        runtime_root_arg=runtime_root_arg,
    )


def write_turn_validated_completion(
    *,
    registry_path: Path,
    runtime_root_arg: str | None,
    goal_id: str,
    todo_id: str,
    completion_turn_key: str,
    evidence: str,
    note: str,
    agent_id: str | None,
    completion_delivery_workspace: Mapping[str, Any] | None = None,
    completion_validation_workspace_path: Path | None = None,
) -> dict[str, Any]:
    """Complete one validated Todo under the effective runtime root."""

    return complete_goal_todo(
        registry_path=registry_path,
        goal_id=goal_id,
        todo_id=todo_id,
        role="agent",
        completion_turn_key=completion_turn_key,
        completion_identity_source="turn_settlement",
        completion_delivery_workspace=completion_delivery_workspace,
        completion_validation_workspace_path=completion_validation_workspace_path,
        evidence=evidence,
        note=note,
        agent_id=agent_id,
        project=None,
        dry_run=False,
        runtime_root_arg=runtime_root_arg,
    )
