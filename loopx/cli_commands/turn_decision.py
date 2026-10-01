"""One shared fresh Turn decision for every Turn subcommand that needs one.

``run-once`` and ``managed-step`` must agree on what "the current governing
decision" means: read the same live status, apply the same controller advisory
primary, and sign the same ``loopx_turn_envelope_v0``. Duplicating that chain
would let the two drift, so both owners build through this module.

The owner carries the inputs the decision was derived from as well, because a
Turn that later spends quota or re-checks the scheduler must use the same live
status, the same scheduler context and the same activation-bound capability
hooks as the decision it already took. A call site that re-derives one of those
inputs is free to drift from the decision it is settling.

Nothing here executes, writes, or spends: it only projects the control plane's
current decision into the envelope the loop controller consumes.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..capabilities.explore.composition_frontier import (
    project_live_explore_composition_frontier,
)
from ..capabilities.periodic_report.pending_intent import (
    periodic_report_pending_intent_interaction_hook,
)
from ..control_plane.quota.live_decision import build_live_quota_should_run_decision
from ..control_plane.quota.turn_envelope import build_turn_envelope
from ..control_plane.scheduler.execution_context import (
    scheduler_execution_context_for_turn,
)
from ..status import AUTONOMOUS_REPLAN_PERIODIC_LOOKBACK, collect_status
from .lark_inbox import build_lark_operator_inbox_urgency_projector
from .turn_selection import turn_controller_advisory_primary

#: The route source every Turn owner attributes its decision to.
TURN_DECISION_ROUTE_SOURCE = "loopx_turn_plan"


def collect_turn_status_payload(
    args: argparse.Namespace,
    *,
    registry_path: Path,
    runtime_root_arg: str | None,
) -> dict[str, Any]:
    """Read the live status the Turn decision is derived from."""

    scan_roots = [Path(item).expanduser() for item in args.scan_path]
    if not scan_roots:
        scan_roots = [Path(args.scan_root).expanduser()]
    return collect_status(
        registry_path=registry_path,
        runtime_root_override=runtime_root_arg,
        scan_roots=scan_roots,
        limit=max(max(0, args.limit), AUTONOMOUS_REPLAN_PERIODIC_LOOKBACK),
        goal_id=args.goal_id,
        available_capabilities=args.available_capabilities,
        agent_lane_id=args.agent_id,
    )


def _build_turn_decision(
    args: argparse.Namespace,
    *,
    registry_path: Path,
    runtime_root: Path,
    status_payload: Mapping[str, Any],
    scheduler_execution_context: Mapping[str, Any],
    operator_inbox_urgency_projector: Callable[..., dict[str, Any]],
    turn_start_hook_dispatch: Mapping[str, Any] | None = None,
) -> Callable[..., dict[str, Any]]:
    """Return the ``build_turn_decision`` every Turn owner resolves through.

    ``turn_start_hook_dispatch`` is the caller's business: an executing Turn
    may publish Go/No-Go hooks before deciding, while a read-only managed step
    must not. Passing the projection in keeps that choice with the caller.
    Every other input is the owner's, so the two commands cannot disagree about
    the status, the scheduler context or the capability hooks behind a decision.
    """

    def build_turn_decision(
        *, requested_action_todo_id: str | None = None
    ) -> dict[str, Any]:
        return build_live_quota_should_run_decision(
            status_payload,
            goal_id=args.goal_id,
            agent_id=args.agent_id,
            available_capabilities=args.available_capabilities,
            include_scheduler_detail=False,
            registry_path=registry_path,
            runtime_root=runtime_root,
            route_source=TURN_DECISION_ROUTE_SOURCE,
            scheduler_execution_context=scheduler_execution_context,
            operator_inbox_urgency_projector=operator_inbox_urgency_projector,
            bounded_research_frontier_projector=(
                project_live_explore_composition_frontier
            ),
            requested_action_todo_id=requested_action_todo_id,
            turn_start_hook_dispatch=dict(turn_start_hook_dispatch or {}),
            interaction_projection_hooks=(
                periodic_report_pending_intent_interaction_hook(
                    registry_path=registry_path,
                    runtime_root=runtime_root,
                    goal_id=args.goal_id,
                    agent_id=args.agent_id,
                ),
            ),
        )

    return build_turn_decision


def apply_controller_advisory_primary(
    build_turn_decision: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Build the decision, preferring the controller's advisory primary Todo."""

    decision = build_turn_decision()
    controller_default = turn_controller_advisory_primary(decision)
    if controller_default is None:
        return decision
    primary_todo_id, advisory_portfolio = controller_default
    decision = build_turn_decision(requested_action_todo_id=primary_todo_id)
    selected_todo = decision.get("selected_todo")
    if not isinstance(selected_todo, dict) or (
        selected_todo.get("todo_id") != primary_todo_id
    ):
        raise ValueError(
            "Turn controller advisory primary failed current eligibility"
        )
    selected_todo["selected_by"] = "turn_controller_advisory_primary"
    decision["action_portfolio"] = advisory_portfolio
    return decision


@dataclass(frozen=True)
class FreshTurnDecisionOwner:
    """The one owner of a fresh Turn decision and the inputs behind it.

    A Turn that already decided must settle against what it decided with: the
    same live status, the same scheduler context and the same
    activation-bound operator-inbox projector. Carrying them here is what keeps
    a later spend or scheduler re-check on the decision's own inputs instead of
    a second reading of the same arguments.
    """

    status_payload: Mapping[str, Any]
    scheduler_execution_context: Mapping[str, Any]
    operator_inbox_urgency_projector: Callable[..., dict[str, Any]]
    build_turn_decision: Callable[..., dict[str, Any]]
    requested_todo_id: str | None = None

    def resolve(self) -> dict[str, Any]:
        """Use an explicit eligible Todo, or preserve controller selection."""

        if self.requested_todo_id is None:
            return apply_controller_advisory_primary(self.build_turn_decision)
        decision = self.build_turn_decision(requested_action_todo_id=self.requested_todo_id)
        selected = decision.get("selected_todo")
        if not isinstance(selected, dict) or selected.get("todo_id") != self.requested_todo_id:
            raise ValueError("Requested Turn Todo is not currently eligible; no alternate task was selected")
        selected["selected_by"] = "turn_explicit_todo"
        return decision


def build_fresh_turn_decision_owner(
    args: argparse.Namespace,
    *,
    registry_path: Path,
    runtime_root: Path,
    runtime_root_arg: str | None,
    turn_start_hook_dispatch: Mapping[str, Any] | None = None,
) -> FreshTurnDecisionOwner:
    """Read the live status and derive the shared decision inputs from it.

    ``runtime_root_arg`` is only the override the status read forwards, as
    ``collect_status`` documents. The root every activation check uses is the
    resolved ``runtime_root``: when a registry declares
    ``common_runtime_root`` and the command omits ``--runtime-root``, the raw
    argument is None and the check would otherwise read the operator's global
    extension state instead of this registry's own.
    """

    status_payload = collect_turn_status_payload(
        args,
        registry_path=registry_path,
        runtime_root_arg=runtime_root_arg,
    )
    scheduler_execution_context = scheduler_execution_context_for_turn(
        host=args.host,
        execution_mode=args.execution_mode,
        scheduler_owner=args.scheduler_owner,
    )
    operator_inbox_urgency_projector = build_lark_operator_inbox_urgency_projector(
        runtime_root_arg=runtime_root,
    )
    return FreshTurnDecisionOwner(
        status_payload=status_payload,
        scheduler_execution_context=scheduler_execution_context,
        operator_inbox_urgency_projector=operator_inbox_urgency_projector,
        build_turn_decision=_with_durable_todo_note(
            _build_turn_decision(
                args,
                registry_path=registry_path,
                runtime_root=runtime_root,
                status_payload=status_payload,
                scheduler_execution_context=scheduler_execution_context,
                operator_inbox_urgency_projector=operator_inbox_urgency_projector,
                turn_start_hook_dispatch=turn_start_hook_dispatch,
            ),
            registry_path=registry_path,
            runtime_root=runtime_root,
            goal_id=args.goal_id,
        ),
        requested_todo_id=getattr(args, "todo_id", None),
    )


def turn_lane_todo_id(
    registry_path: Path, goal_id: str, agent_id: str, selected_todo: Mapping[str, Any],
) -> str | None:
    """The todo whose (goal, todo) lane this Turn runs in, else ``None``.

    Fork role_v1 (design-v0 decision 32): developer and acceptor Turns are
    fenced per todo, so one agent can run several todos of a goal at once.
    The orchestrator, peer_v1 goals and todo-less Turns keep the agent lane.
    """

    from ..agent_registry import agent_role_for_goal, load_goal_from_registry
    from ..control_plane.turn_driver.lane_fence import turn_lane_todo_scope
    from ..todo_acceptance import goal_uses_role_v1

    try:
        goal = load_goal_from_registry(registry_path, goal_id)
    except (OSError, ValueError):
        return None
    return turn_lane_todo_scope(
        role_v1=goal_uses_role_v1(goal),
        agent_role=agent_role_for_goal(goal, agent_id),
        todo_id=str(selected_todo.get("todo_id") or ""),
    )


def registry_goal_uses_role_v1(registry_path: Path, goal_id: str) -> bool:
    from ..agent_registry import load_goal_from_registry
    from ..todo_acceptance import goal_uses_role_v1

    try:
        return goal_uses_role_v1(load_goal_from_registry(registry_path, goal_id))
    except (OSError, ValueError):
        return False


def _goal_acceptance_brief(contract: Any) -> dict[str, Any] | None:
    """Bounded objective and criteria of an enabled goal acceptance contract."""

    if not isinstance(contract, dict) or contract.get("enabled") is not True:
        return None
    criteria = []
    for row in contract.get("criteria") or []:
        if isinstance(row, dict) and (row.get("id") or row.get("description")):
            label = " ".join(f"{row.get('id') or ''}: {row.get('description') or ''}".split())
            criteria.append(label.strip(": ")[:240])
    objective = " ".join(str(contract.get("objective") or "").split())[:300]
    if not objective and not criteria:
        return None
    return {**({"objective": objective} if objective else {}), "criteria": criteria[:12]}


def _with_durable_todo_note(
    build: Callable[..., dict[str, Any]], *, registry_path: Path, runtime_root: Path, goal_id: str,
) -> Callable[..., dict[str, Any]]:
    """Attach the selected todo's durable acceptance context before signing.

    Fork role_v1: the status projection behind the decision carries no notes
    and no acceptance criteria, so the host never saw them (E2E pilot). The
    selected todo gets its bounded note, its orchestrator-owned
    ``acceptance_criteria`` (G2) and the goal-level acceptance contract when
    one is enabled (decision 9). Only on a role_v1 goal: other goals get the
    builder unchanged, with no extra todo read and no envelope change.
    """

    if not registry_goal_uses_role_v1(registry_path, goal_id):
        return build

    def wrapped(**kwargs: Any) -> dict[str, Any]:
        decision = build(**kwargs)
        selected = decision.get("selected_todo") if isinstance(decision, dict) else None
        todo_id = selected.get("todo_id") if isinstance(selected, dict) else None
        if not todo_id:
            return decision
        from ..todos import list_goal_todos

        try:
            listed = list_goal_todos(
                registry_path=registry_path, goal_id=goal_id, todo_id=str(todo_id),
                runtime_root_arg=str(runtime_root),
            )
        except (OSError, ValueError):
            return decision
        for item in listed.get("todos") or []:
            if isinstance(item, dict) and item.get("todo_id") == todo_id:
                for field, limit in (("note", 600), ("acceptance_criteria", 1000)):
                    value = " ".join(str(item.get(field) or "").split())
                    if value and not selected.get(field):
                        selected[field] = value[:limit]
                break
        agent_todos = listed.get("agent_todos")
        brief = _goal_acceptance_brief(
            agent_todos.get("goal_acceptance_contract") if isinstance(agent_todos, dict) else None
        )
        if brief and not selected.get("goal_acceptance"):
            selected["goal_acceptance"] = brief
        return decision

    return wrapped


def fresh_turn_envelope(
    decision: Mapping[str, Any],
    *,
    scheduler_execution_context: Any,
) -> dict[str, Any]:
    """Sign the decision into the envelope the loop controller consumes.

    The causal link to a failed predecessor is *not* written here: the envelope
    schema is a signed contract, so the managed step passes its predecessor
    beside the envelope instead of mutating it.
    """

    return build_turn_envelope(
        decision,
        scheduler_execution_context=scheduler_execution_context,
    )


def build_fresh_envelope_for_managed_step(
    args: argparse.Namespace,
    *,
    registry_path: Path,
    runtime_root: Path,
    runtime_root_arg: str | None,
) -> dict[str, Any]:
    """Project the current decision as the managed step's fresh envelope.

    Read-only: no Turn-start hooks are dispatched and no Turn instance is
    minted, because the managed step only decides whether the outer scheduler
    may wake the same failed Turn again.
    """

    owner = build_fresh_turn_decision_owner(
        args,
        registry_path=registry_path,
        runtime_root=runtime_root,
        runtime_root_arg=runtime_root_arg,
    )
    return fresh_turn_envelope(
        owner.resolve(),
        scheduler_execution_context=owner.scheduler_execution_context,
    )


__all__ = [
    "FreshTurnDecisionOwner",
    "TURN_DECISION_ROUTE_SOURCE",
    "apply_controller_advisory_primary",
    "build_fresh_envelope_for_managed_step",
    "build_fresh_turn_decision_owner",
    "collect_turn_status_payload",
    "fresh_turn_envelope",
]
