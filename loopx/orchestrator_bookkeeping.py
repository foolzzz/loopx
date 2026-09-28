"""Mechanical orchestrator bookkeeping done by LoopX itself (design decision 43).

The E2E pilot v1 usage ledger showed orchestrator Turns that only did
bookkeeping: after the user approved the plan card, a whole Fable Turn
($0.85) launched just to return ``validated_completion`` for the intake
planning todo, which LoopX then verified with ``plan list --require-status
applied``. LoopX already knew the answer. This module closes such todos
deterministically, through the ordinary Todo completion (attributed to the
todo's claim owner, the orchestrator) with the evidence the model would have
reported, so no model Turn is launched for them.

What is mechanical, and closed here:

* **planning closeout** - an open orchestrator planning todo
  (``action_kind=plan``) once a plan card that creates todos is approved and
  applied. Done right at apply time (:func:`close_planning_todos_after_apply`)
  and, as a backstop for older goals or a failed closeout, before the
  dispatcher would launch the orchestrator for it;
* **superseded escalation** - an S2 escalation todo whose escalated todo is
  already ``done`` (accepted, superseded or closed by the owner): nothing is
  left to decide;
* **answered gate action todo** - a dispatcher action todo for gate replies
  when every listed gate's thread was already answered by the orchestrator
  (another Turn replied), or the gate is a LoopX system gate
  (``acceptor_blocked``, ``push_request``, ``budget_exhausted``,
  ``goal_complete``) the user has already closed: LoopX settled that decision
  and any work it adds arrives as its own todo.

What stays a model Turn (judgment): planning and clarification, replying to a
user's gate message, a closed question or plan gate whose last message is the
user's, escalation decisions on a still-blocked todo, criteria-change
outcomes, replan obligations and user follow-ups.

A closeout is an ordinary validated Todo write; if it fails, nothing changes
and the orchestrator Turn runs as before.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

PLANNING_ACTION_KIND = "plan"
BOOKKEEPING_KIND_PLANNING = "planning_closeout"
BOOKKEEPING_KIND_ESCALATION = "escalation_target_closed"
BOOKKEEPING_KIND_GATE_ACTION = "gate_action_answered"
_EVIDENCE_LIMIT = 480
_NOTE_PREFIX = "Closed by LoopX without a model Turn (decision 43): "
# LoopX settles these gates' decisions itself; a closed one needs no orchestrator follow-up.
_SYSTEM_SETTLED_GATE_KINDS = frozenset({"acceptor_blocked", "push_request", "budget_exhausted", "goal_complete"})


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _row_role(row: Mapping[str, Any]) -> str:
    return str(row.get("role") or "agent")


def is_orchestrator_planning_todo(row: Mapping[str, Any] | None, orchestrator: str | None = None) -> bool:
    """An agent todo that asks the orchestrator to plan (the intake planning todo)."""

    if not isinstance(row, Mapping) or _row_role(row) != "agent":
        return False
    if str(row.get("action_kind") or "") != PLANNING_ACTION_KIND:
        return False
    required = str(row.get("required_role") or "")
    if required:
        return required == "orchestrator"
    return bool(orchestrator) and row.get("claimed_by") == orchestrator


def _goal(registry_path: Path, goal_id: str) -> dict[str, Any] | None:
    from .agent_registry import load_goal_from_registry

    return load_goal_from_registry(Path(registry_path), goal_id)


def _rows(registry_path: Path, goal_id: str, runtime_root_arg: str | None) -> list[dict[str, Any]]:
    from .todos import list_goal_todos

    listed = list_goal_todos(registry_path=registry_path, goal_id=goal_id, runtime_root_arg=runtime_root_arg)
    return [dict(row) for row in listed.get("todos") or [] if isinstance(row, Mapping)]


def _plan_created_todo_ids(plans: Iterable[Mapping[str, Any]]) -> set[str]:
    created: set[str] = set()
    for plan in plans:
        created.update(str(todo_id) for todo_id in (plan.get("todo_id_map") or {}).values() if todo_id)
    return created


def _planning_evidence(record: Mapping[str, Any]) -> str:
    mapping = record.get("todo_id_map") or {}
    created = ", ".join(f"{key}={todo_id}" for key, todo_id in mapping.items())
    return _clip(
        f"plan_applied={record.get('plan_id')} rev={int(record.get('revision') or 1)} "
        f"gate={record.get('gate_todo_id')} created_todos={len(mapping)}: {created}",
        _EVIDENCE_LIMIT,
    )


def _complete(
    *, registry_path: Path, goal_id: str, row: Mapping[str, Any], orchestrator: str | None,
    evidence: str, note: str, runtime_root_arg: str | None,
) -> dict[str, Any]:
    from .todos import complete_goal_todo

    todo_id = str(row.get("todo_id"))
    try:
        result = complete_goal_todo(
            registry_path=registry_path, goal_id=goal_id, todo_id=todo_id, runtime_root_arg=runtime_root_arg,
            role="agent", agent_id=str(row.get("claimed_by") or orchestrator or "") or None,
            evidence=evidence, note=_clip(_NOTE_PREFIX + note, 400), no_followup=True,
        )
    except Exception as exc:  # noqa: BLE001 - a failed closeout leaves the todo to a model Turn
        return {"todo_id": todo_id, "closed": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    if result.get("ok") is False or not result.get("completed", True):
        return {"todo_id": todo_id, "closed": False, "error": str(result.get("error") or "not completed")[:300]}
    return {"todo_id": todo_id, "closed": True, "evidence": evidence}


def close_planning_todos_after_apply(
    *, registry_path: Path, runtime_root: Path, goal_id: str, record: Mapping[str, Any],
    runtime_root_arg: str | None = None,
) -> list[dict[str, Any]]:
    """Complete the open orchestrator planning todo(s) once a plan card is applied.

    Only a card that created todos finishes planning (a criteria-change-only
    card does not). A planning todo that an applied plan created itself (a
    planned later planning step) is left alone: this card did not answer it.
    role_v1 goals only; errors are returned, never raised.
    """

    from .plan_cards import list_plans
    from .todo_acceptance import goal_uses_role_v1

    if not (record.get("todo_id_map") or {}):
        return []
    try:
        goal = _goal(registry_path, goal_id)
        if not goal_uses_role_v1(goal):
            return []
        from .agent_registry import orchestrator_agent_for_goal

        orchestrator = orchestrator_agent_for_goal(goal)
        created = _plan_created_todo_ids(list_plans(runtime_root, goal_id)) | _plan_created_todo_ids([record])
        rows = _rows(registry_path, goal_id, runtime_root_arg)
    except Exception as exc:  # noqa: BLE001 - never fail the plan apply over bookkeeping
        return [{"closed": False, "error": f"{type(exc).__name__}: {exc}"[:300]}]
    closed: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("status") or "") != "open" or str(row.get("todo_id")) in created:
            continue
        if not is_orchestrator_planning_todo(row, orchestrator):
            continue
        outcome = _complete(
            registry_path=registry_path, goal_id=goal_id, row=row, orchestrator=orchestrator,
            evidence=_planning_evidence(record),
            note=f"plan {record.get('plan_id')} was approved and applied.",
            runtime_root_arg=runtime_root_arg,
        )
        closed.append({**outcome, "kind": BOOKKEEPING_KIND_PLANNING})
    return closed


def _latest_applied_plan(runtime_root: Path, goal_id: str) -> tuple[dict[str, Any] | None, set[str]]:
    from .plan_cards import list_plans

    plans = list_plans(runtime_root, goal_id)
    applied = [plan for plan in plans if plan.get("status") == "applied" and plan.get("todo_id_map")]
    return (applied[-1] if applied else None), _plan_created_todo_ids(plans)


def _gate_action_answered(runtime_root: Path, goal_id: str, gate_ids: list[str], rows: Mapping[str, Mapping[str, Any]]) -> str | None:
    """Evidence when every listed gate needs nothing more from the orchestrator."""

    from .gate_threads import AUTHOR_ORCHESTRATOR, read_gate_index

    index = read_gate_index(runtime_root, goal_id)["gates"]
    parts: list[str] = []
    for gate_id in gate_ids:
        entry = index.get(gate_id)
        row = rows.get(gate_id)
        if not isinstance(entry, Mapping) or row is None:
            return None
        closed = bool(entry.get("closed")) or str(row.get("status") or "") != "open"
        if entry.get("last_author") == AUTHOR_ORCHESTRATOR:
            parts.append(f"{gate_id}: answered by the orchestrator{' and closed' if closed else ''}")
        elif closed and str(entry.get("kind") or "") in _SYSTEM_SETTLED_GATE_KINDS:
            parts.append(f"{gate_id}: {entry.get('kind')} gate closed and settled by LoopX")
        else:
            return None
    return "; ".join(parts)


def mechanical_orchestrator_closeout(
    *, registry_path: Path, runtime_root: Path, goal_id: str, todo: Mapping[str, Any],
    runtime_root_arg: str | None = None,
) -> dict[str, Any] | None:
    """Close ``todo`` without a model Turn when it is pure bookkeeping.

    Returns ``None`` when the todo needs the orchestrator's judgment (the
    dispatcher then launches the Turn as before), else the closeout outcome
    ``{todo_id, closed, kind, evidence | error}``.
    """

    from .dispatch.orchestrator_actions import action_gate_ids, is_orchestrator_action_todo
    from .todo_acceptance import escalated_todo_id, goal_uses_role_v1

    if not isinstance(todo, Mapping) or str(todo.get("status") or "") != "open" or _row_role(todo) != "agent":
        return None
    goal = _goal(registry_path, goal_id)
    if not goal_uses_role_v1(goal):
        return None
    from .agent_registry import orchestrator_agent_for_goal

    orchestrator = orchestrator_agent_for_goal(goal)
    todo_id = str(todo.get("todo_id") or "")
    kind: str | None = None
    evidence = note = ""
    if is_orchestrator_planning_todo(todo, orchestrator):
        plan, created = _latest_applied_plan(runtime_root, goal_id)
        if plan is None or todo_id in created:
            return None
        kind, evidence = BOOKKEEPING_KIND_PLANNING, _planning_evidence(plan)
        note = f"plan {plan.get('plan_id')} was already approved and applied."
    elif escalated_todo_id(todo):
        target = escalated_todo_id(todo)
        rows = {str(row.get("todo_id")): row for row in _rows(registry_path, goal_id, runtime_root_arg)}
        row = rows.get(str(target))
        if row is None or str(row.get("status") or "") != "done":
            return None
        from .plan_dependencies import accepted_by, read_supersessions, supersession_map, todo_is_superseded

        if todo_is_superseded(row, supersession_map(read_supersessions(runtime_root, goal_id))):
            how = "superseded"
        elif accepted_by(row):
            how = f"accepted by {accepted_by(row)}"
        else:
            how = "closed"
        kind = BOOKKEEPING_KIND_ESCALATION
        evidence = _clip(f"escalated_todo={target} status=done ({how}); nothing left to decide", _EVIDENCE_LIMIT)
        note = f"the escalated todo {target} was already {how}."
    elif is_orchestrator_action_todo(todo) and action_gate_ids(todo):
        rows = {str(row.get("todo_id")): row for row in _rows(registry_path, goal_id, runtime_root_arg)}
        answered = _gate_action_answered(runtime_root, goal_id, action_gate_ids(todo), rows)
        if not answered:
            return None
        kind, evidence = BOOKKEEPING_KIND_GATE_ACTION, _clip(f"gates: {answered}", _EVIDENCE_LIMIT)
        note = "every listed gate reply was already answered or settled."
    if kind is None:
        return None
    outcome = _complete(
        registry_path=registry_path, goal_id=goal_id, row=todo, orchestrator=orchestrator,
        evidence=evidence, note=note, runtime_root_arg=runtime_root_arg,
    )
    return {**outcome, "kind": kind}
