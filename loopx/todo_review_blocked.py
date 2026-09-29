"""The acceptor's third verdict: blocked (fork gap G12, design decision 36).

An acceptor that cannot review a delivered todo for its own reasons (broken
tooling, environment, missing dependencies) records ``blocked`` instead of
rejecting the delivery:

* the todo stays ``in_review`` and ``reject_count`` is unchanged;
* LoopX immediately opens one system user gate (kind ``acceptor_blocked``)
  that carries the acceptor's reason. This is the same class of exception to
  decision 11 as the dispatcher's re-login gate (decision 17): the gate is
  opened by LoopX, not by the acceptor, and it blocks the acceptor's lane;
* the dispatcher does not relaunch the acceptor on that todo while the gate
  is open.

The owner resolves the gate with one of four options:

============================  ===========  ====================================
option                        decision     effect
============================  ===========  ====================================
``retry_acceptance``          approve      the todo stays ``in_review``; the
                                           acceptor may review it again
``accept_manually``           approve      merge first, then complete, with the
                                           owner as the verdict actor
``return_to_developer``       reject       reopen with the gate note as
                                           ``review_feedback``; no count
``cancel_todo``               cancel       supersede the todo
============================  ===========  ====================================

``approve`` without an option means ``retry_acceptance``. The gate closes
through the ordinary gate decision path (``loopx gate resolve``, ``loopx todo
complete --role user --decision-outcome``, or the dashboard ``gate.resolve``);
``plan_cards.gate_decision_preflight`` and ``settle_gate_decision`` route
this gate kind here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .agent_registry import lifecycle_agent_for_owner_write, load_goal_from_registry
from .control_plane.todos.contract import (
    TODO_STATUS_DONE,
    TODO_STATUS_IN_REVIEW,
    TODO_STATUS_OPEN,
    compact_todo_text,
    normalize_todo_claimed_by,
    normalize_todo_status,
)
from .gate_threads import GATE_KIND_ACCEPTOR_BLOCKED, read_gate_index, register_gate_kind, run_gate_settlement
from .todo_acceptance import (
    TODO_ACCEPTANCE_SCHEMA_VERSION,
    _clip,
    _read_todo,
    _require_verdict_actor,
    accept_delivered_todo,
    goal_uses_role_v1,
    resolve_todo_acceptor,
    resume_after_accept,
)

REVIEW_GATE_TEXT_PREFIX = "Acceptor blocked: "
OPTION_RETRY_ACCEPTANCE = "retry_acceptance"
OPTION_ACCEPT_MANUALLY = "accept_manually"
OPTION_RETURN_TO_DEVELOPER = "return_to_developer"
OPTION_CANCEL_TODO = "cancel_todo"
REVIEW_GATE_OPTIONS = (
    OPTION_RETRY_ACCEPTANCE, OPTION_ACCEPT_MANUALLY, OPTION_RETURN_TO_DEVELOPER, OPTION_CANCEL_TODO,
)
REVIEW_GATE_OPTION_DECISIONS = {
    OPTION_RETRY_ACCEPTANCE: "approve",
    OPTION_ACCEPT_MANUALLY: "approve",
    OPTION_RETURN_TO_DEVELOPER: "reject",
    OPTION_CANCEL_TODO: "cancel",
}
_DEFAULT_OPTION_FOR_DECISION = {
    None: OPTION_RETRY_ACCEPTANCE,
    "approve": OPTION_RETRY_ACCEPTANCE,
    "reject": OPTION_RETURN_TO_DEVELOPER,
    "cancel": OPTION_CANCEL_TODO,
}
OWNER_ACTOR = "owner"
# The gate index key of a settled acceptor_blocked gate's outcome (first settlement wins).
REVIEW_OUTCOME_KEY = "review_outcome"
_REVIEW_OUTCOME_FIELDS = ("ok", "gate_todo_id", "review_todo_id", "option", "applied", "reason", "error",
                          "review_feedback")
_REASON_LIMIT = 300


def _runtime_root(registry_path: Path, runtime_root_arg: str | None) -> Path:
    from .control_plane.coordination.local_authority_shadow_adapter import effective_runtime_root

    return effective_runtime_root(registry_path, runtime_root_arg)


def _open_user_todo_ids(registry_path: Path, goal_id: str, runtime_root_arg: str | None) -> set[str]:
    from .todos import list_goal_todos

    listed = list_goal_todos(
        registry_path=registry_path, goal_id=goal_id, role="user", status="open",
        runtime_root_arg=runtime_root_arg,
    )
    return {str(item.get("todo_id")) for item in listed.get("todos") or [] if isinstance(item, Mapping)}


def review_gate_entry(runtime_root: Path, goal_id: str, gate_todo_id: str) -> dict[str, Any] | None:
    """The gate index entry of an ``acceptor_blocked`` gate, else None."""

    entry = read_gate_index(runtime_root, goal_id)["gates"].get(str(gate_todo_id))
    if isinstance(entry, Mapping) and entry.get("kind") == GATE_KIND_ACCEPTOR_BLOCKED:
        return dict(entry)
    return None


def open_review_gates(
    runtime_root: Path, goal_id: str, open_user_todo_ids: set[str] | frozenset[str],
) -> dict[str, str]:
    """``{review todo id: gate todo id}`` for every open ``acceptor_blocked`` gate."""

    found: dict[str, str] = {}
    for gate_id, entry in read_gate_index(runtime_root, goal_id)["gates"].items():
        if (
            isinstance(entry, Mapping) and entry.get("kind") == GATE_KIND_ACCEPTOR_BLOCKED
            and not entry.get("closed") and gate_id in open_user_todo_ids and entry.get("review_todo_id")
        ):
            found[str(entry["review_todo_id"])] = str(gate_id)
    return found


def _event(runtime_root: Path, goal_id: str, event_kind: str, *, todo_id: str, agent_id: str | None,
           status: str, details: Mapping[str, Any]) -> None:
    from .rollout_event_log import append_rollout_event, build_rollout_event, rollout_event_log_path

    try:
        append_rollout_event(
            rollout_event_log_path(runtime_root, goal_id),
            build_rollout_event(
                goal_id=goal_id, event_kind=event_kind, agent_id=agent_id, todo_id=todo_id,
                status=status, details=dict(details),
            ),
        )
    except (OSError, ValueError):
        pass  # the event log is an audit trail; the state write already landed


def block_goal_todo_review(
    *, registry_path: Path, goal_id: str, todo_id: str, agent_id: str | None, reason: str | None,
    runtime_root_arg: str | None = None, project: Path | None = None, state_file: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Acceptor verdict ``blocked``: keep the todo ``in_review`` and open a user gate."""

    stated = compact_todo_text(reason)
    if not stated:
        raise ValueError("todo block-review requires --reason: why the acceptor cannot review")
    goal = load_goal_from_registry(registry_path, goal_id)
    todo = _read_todo(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id,
        runtime_root_arg=runtime_root_arg, project=project, state_file=state_file,
    )
    if todo is None:
        raise ValueError(f"todo_id {todo_id!r} was not found")
    actor, reviewer = _require_verdict_actor(goal=goal, todo=todo, agent_id=agent_id, verb="block-review")
    runtime_root = _runtime_root(registry_path, runtime_root_arg)
    acceptance: dict[str, Any] = {
        "schema_version": TODO_ACCEPTANCE_SCHEMA_VERSION, "transition": "review_blocked",
        "verdict": "blocked", "acceptor": actor, "acceptor_source": reviewer["source"],
        "reason": _clip(stated, _REASON_LIMIT), "status": TODO_STATUS_IN_REVIEW,
        "reject_count_unchanged": True, "options": list(REVIEW_GATE_OPTIONS),
    }
    existing = open_review_gates(
        runtime_root, goal_id, _open_user_todo_ids(registry_path, goal_id, runtime_root_arg),
    ).get(str(todo["todo_id"]))
    if existing:
        return {"ok": True, "dry_run": dry_run, "changed": False, "goal_id": goal_id, "todo_id": todo_id,
                "acceptance": {**acceptance, "gate_todo_id": existing, "idempotent_replay": True}}
    text = (
        f"{REVIEW_GATE_TEXT_PREFIX}{actor} cannot review {todo['todo_id']} "
        f"({compact_todo_text(todo.get('text'))[:120]}). Reason: {_clip(stated, _REASON_LIMIT)}. "
        "Choose one: retry acceptance (after fixing the environment), accept manually, "
        "return to developer, or cancel the todo "
        "(`loopx gate resolve --option retry_acceptance|accept_manually|return_to_developer|cancel_todo`)."
    )
    if dry_run:
        return {"ok": True, "dry_run": True, "changed": False, "goal_id": goal_id, "todo_id": todo_id,
                "acceptance": {**acceptance, "gate_text": text}}
    from .todos import add_goal_todo

    # The owner path (no agent id): LoopX opens this gate, not the acceptor.
    gate = add_goal_todo(
        registry_path=registry_path, goal_id=goal_id, role="user", text=text,
        task_class="user_gate", blocks_agent=actor,
        note="Opened by LoopX: the acceptor could not review a delivered todo.",
        runtime_root_arg=runtime_root_arg, project=project, state_file=state_file,
    )
    gate_id = str(gate.get("todo_id") or "")
    register_gate_kind(
        runtime_root, goal_id, gate_id, kind=GATE_KIND_ACCEPTOR_BLOCKED,
        extra={"review_todo_id": str(todo["todo_id"]), "acceptor_agent": actor,
               "options": list(REVIEW_GATE_OPTIONS)},
    )
    _event(runtime_root, goal_id, "todo_review_blocked", todo_id=str(todo["todo_id"]), agent_id=actor,
           status=TODO_STATUS_IN_REVIEW, details={"gate_id": gate_id, "reject_count_unchanged": True})
    return {"ok": True, "dry_run": False, "changed": True, "goal_id": goal_id, "todo_id": todo_id,
            "acceptance": {**acceptance, "gate_todo_id": gate_id}}


def block_review_from_turn(
    *, registry_path: Path, goal_id: str, todo_id: str, agent_id: str, reason: str,
    runtime_root_arg: str | None = None,
) -> dict[str, Any] | None:
    """Record an acceptor Turn's blocked verdict; None when it does not apply.

    Applies only when the todo is ``in_review`` on a role_v1 goal and
    ``agent_id`` is its resolved acceptor, so other Turns are unaffected.
    """

    goal = load_goal_from_registry(registry_path, goal_id)
    if not goal_uses_role_v1(goal):
        return None
    todo = _read_todo(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id,
        runtime_root_arg=runtime_root_arg, project=None, state_file=None,
    )
    actor = normalize_todo_claimed_by(agent_id)
    if (
        todo is None or not actor
        or normalize_todo_status(todo.get("status")) != TODO_STATUS_IN_REVIEW
        or actor != resolve_todo_acceptor(goal, todo)["agent_id"]
    ):
        return None
    return block_goal_todo_review(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id, agent_id=actor,
        reason=compact_todo_text(reason) or "the acceptor Turn could not review and gave no reason",
        runtime_root_arg=runtime_root_arg,
    )


def settle_turn_stop_verdict(
    payload: Mapping[str, Any], *, registry_path: Path, runtime_root: Path, runtime_root_arg: str | None,
    goal_id: str, agent_id: str, selected_todo: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Map an acceptor Turn's ``user_action_required`` stop to the blocked verdict.

    ``user_action_required`` is the Turn result kind for "this needs a human";
    for the acceptor of an ``in_review`` todo it means the acceptor cannot
    review, and its ``summary`` is the reason.
    """

    todo_id = str(selected_todo.get("todo_id") or "")
    if (
        not todo_id or payload.get("status") != "stopped"
        or payload.get("result_kind") != "user_action_required"
        or str(selected_todo.get("status") or "") != TODO_STATUS_IN_REVIEW
    ):
        return None
    summary = ""
    turn_key = str(payload.get("resume_turn_key") or "")
    if turn_key:
        from .control_plane.turn_driver.journal_store import load_turn_journal, turn_journal_path

        try:
            journal = load_turn_journal(turn_journal_path(runtime_root, goal_id=goal_id, turn_key=turn_key)) or {}
        except (OSError, ValueError):
            journal = {}
        host_result = journal.get("host_result") if isinstance(journal.get("host_result"), Mapping) else {}
        summary = str(host_result.get("summary") or host_result.get("classification") or "")
    try:
        blocked = block_review_from_turn(
            registry_path=registry_path, goal_id=goal_id, todo_id=todo_id, agent_id=agent_id,
            reason=summary, runtime_root_arg=runtime_root_arg,
        )
    except (OSError, ValueError) as error:  # the Turn itself settled; report the verdict failure
        return {"verdict": "blocked", "ok": False, "error": str(error)[:400]}
    return blocked.get("acceptance") if isinstance(blocked, Mapping) else None


# --- gate resolution ----------------------------------------------------------


def resolve_review_gate_option(decision: str | None, option: str | None) -> str:
    """The option a gate decision selects; an explicit option must match its decision."""

    if option is None:
        if decision not in _DEFAULT_OPTION_FOR_DECISION:
            raise ValueError(f"unsupported gate decision {decision!r} for an acceptor-blocked gate")
        return _DEFAULT_OPTION_FOR_DECISION[decision]
    if option not in REVIEW_GATE_OPTION_DECISIONS:
        raise ValueError(f"gate option must be one of: {', '.join(REVIEW_GATE_OPTIONS)}")
    if decision is not None and REVIEW_GATE_OPTION_DECISIONS[option] != decision:
        raise ValueError(
            f"gate option {option} records decision {REVIEW_GATE_OPTION_DECISIONS[option]}, not {decision}"
        )
    return option


def review_gate_preflight(
    *, registry_path: Path, runtime_root: Path, goal_id: str, gate_todo_id: str,
    decision: str | None, option: str | None, runtime_root_arg: str | None = None,
) -> str | None:
    """Validate a decision on an ``acceptor_blocked`` gate before it closes.

    Returns the selected option, or None for other gates (where an option is
    refused). ``accept_manually`` needs the todo still ``in_review``, so a
    stale choice leaves the gate open.
    """

    entry = review_gate_entry(runtime_root, goal_id, gate_todo_id)
    if entry is None:
        if option is not None:
            raise ValueError(
                "a gate option applies only to an acceptor-blocked, budget_exhausted or goal_complete gate"
            )
        return None
    selected = resolve_review_gate_option(decision, option)
    if selected == OPTION_ACCEPT_MANUALLY:
        todo = _read_todo(
            registry_path=registry_path, goal_id=goal_id, todo_id=str(entry.get("review_todo_id") or ""),
            runtime_root_arg=runtime_root_arg, project=None, state_file=None,
        )
        status = normalize_todo_status((todo or {}).get("status"))
        if status != TODO_STATUS_IN_REVIEW:
            raise ValueError(
                f"cannot accept manually: todo {entry.get('review_todo_id')!r} is {status!r}, not in_review"
            )
    return selected


def settle_review_gate(
    *, registry_path: Path, runtime_root: Path, goal_id: str, gate_todo_id: str,
    decision: str | None, option: str | None, note: str | None = None,
    runtime_root_arg: str | None = None,
) -> dict[str, Any] | None:
    """After an ``acceptor_blocked`` gate closed, apply the chosen option.

    Returns None for other gates. One settlement per gate runs at a time: a
    recorded outcome replays and applies nothing, and only a failed one is
    retried (with its recorded option); see ``gate_threads.run_gate_settlement``.
    The index keeps a compact ``review_outcome``; the returned payload of a
    settlement that ran also carries the acceptance and merge details.
    """

    entry = review_gate_entry(runtime_root, goal_id, gate_todo_id)
    if entry is None:
        return None
    ran: dict[str, Any] = {}

    def apply(_decided: str | None, selected: str, _prior: Mapping[str, Any]) -> dict[str, Any]:
        ran.update(_apply_review_option(
            registry_path=registry_path, goal_id=goal_id, gate_todo_id=gate_todo_id, entry=entry,
            selected=selected, note=note, runtime_root_arg=runtime_root_arg,
        ))
        return {key: ran[key] for key in _REVIEW_OUTCOME_FIELDS if key in ran}

    outcome, applied = run_gate_settlement(
        runtime_root, goal_id, gate_todo_id, decision=decision, option=resolve_review_gate_option(decision, option),
        outcome_key=REVIEW_OUTCOME_KEY, apply=apply,
    )
    if not applied:
        return {"payload_key": "review_gate", **outcome, "replayed": True}
    if "reason" not in ran:  # the option's todo write was attempted
        _event(runtime_root, goal_id, "review_gate_decided", todo_id=ran["review_todo_id"],
               agent_id=str(entry.get("acceptor_agent") or "") or None, status=ran["option"],
               details={"gate_id": gate_todo_id, "option": ran["option"], "applied": ran["applied"]})
    return {"payload_key": "review_gate", **ran}


def _apply_review_option(
    *, registry_path: Path, goal_id: str, gate_todo_id: str, entry: Mapping[str, Any], selected: str,
    note: str | None, runtime_root_arg: str | None,
) -> dict[str, Any]:
    """Apply one acceptor-blocked gate option to its review todo; report, never raise."""

    review_todo_id = str(entry.get("review_todo_id") or "")
    acceptor = str(entry.get("acceptor_agent") or "")
    goal = load_goal_from_registry(registry_path, goal_id)
    todo = _read_todo(
        registry_path=registry_path, goal_id=goal_id, todo_id=review_todo_id,
        runtime_root_arg=runtime_root_arg, project=None, state_file=None,
    )
    result: dict[str, Any] = {
        "ok": True, "gate_todo_id": gate_todo_id, "review_todo_id": review_todo_id, "option": selected,
        "applied": False,
    }
    status = normalize_todo_status((todo or {}).get("status"))
    if selected == OPTION_ACCEPT_MANUALLY and todo is not None and status == TODO_STATUS_DONE:
        from .plan_dependencies import accepted_by

        if accepted_by(todo) == OWNER_ACTOR:
            # The owner's accept through this gate completed the todo, but the
            # settlement did not finish: re-run the idempotent post-accept step.
            try:
                resumed = resume_after_accept(registry_path=registry_path, goal_id=goal_id,
                                              runtime_root_arg=runtime_root_arg)
                result.update(applied=True, **({"resumed_todo_ids": resumed} if resumed else {}))
            except (OSError, ValueError) as error:  # the gate is closed; report, never raise
                result.update(ok=False, error=str(error)[:400])
            return result
    if todo is None or status != TODO_STATUS_IN_REVIEW:
        # Somebody already moved the todo (for example a later verdict).
        result["reason"] = f"todo is {status or 'missing'}, not in_review; nothing to apply"
        return result
    # The owner is not an agent: every option's todo write is attributed to the
    # claim owner, else the blocked acceptor, else the orchestrator.
    owner = lifecycle_agent_for_owner_write(dict(goal or {}), todo.get("claimed_by"), acceptor)
    gate_note = compact_todo_text(note)
    try:
        if selected == OPTION_RETRY_ACCEPTANCE:
            result["applied"] = True
        elif selected == OPTION_ACCEPT_MANUALLY:
            accepted = accept_delivered_todo(
                registry_path=registry_path, goal=goal, goal_id=goal_id, todo=todo, actor=OWNER_ACTOR,
                actor_source="owner_via_acceptor_blocked_gate",
                note=gate_note or f"accepted manually by the owner after {acceptor} could not review",
                runtime_root_arg=runtime_root_arg, lifecycle_agent_id=owner,
            )
            result.update(applied=accepted.get("ok") is not False, acceptance=accepted.get("acceptance"),
                          **({"merge": accepted["merge"]} if "merge" in accepted else {}))
            if accepted.get("ok") is False:  # the todo stays in_review; a retry re-runs the accept
                result.update(ok=False, error=str(accepted.get("error") or "the manual acceptance did not complete")[:400])
        elif selected == OPTION_RETURN_TO_DEVELOPER:
            from .todos import update_goal_todo

            feedback = _clip(
                f"returned to the developer by the owner after acceptor {acceptor} could not review: "
                f"{gate_note or 'see the acceptor-blocked gate ' + gate_todo_id}", 600,
            )
            update_goal_todo(
                registry_path=registry_path, goal_id=goal_id, todo_id=review_todo_id, role="agent",
                runtime_root_arg=runtime_root_arg, status=TODO_STATUS_OPEN,
                role_contract={"review_feedback": feedback}, agent_id=owner, dry_run=False,
            )
            result.update(applied=True, review_feedback=feedback)
        else:
            from .todos import supersede_goal_todo

            supersede_goal_todo(
                registry_path=registry_path, goal_id=goal_id, todo_id=review_todo_id, role="agent",
                runtime_root_arg=runtime_root_arg, agent_id=owner,
                reason=_clip(f"cancelled by the owner after acceptor {acceptor} could not review", 200),
            )
            result["applied"] = True
    except (OSError, ValueError) as error:  # the gate is closed; report, never raise
        result.update(ok=False, error=str(error)[:400])
    return result
