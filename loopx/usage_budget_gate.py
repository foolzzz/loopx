"""Pause a goal's new Turns when its usage budget is exhausted (design decision 41).

Budgets are optional (``loopx usage budget --goal G --set USD``). With a
budget, the dispatcher keeps the non-blocking 80% alert
(``loopx.dispatch.usage_budget``); once the recorded spend reaches 100% it
opens one system user gate of kind ``budget_exhausted`` per goal and budget
crossing, like the re-login (decision 17), acceptor-blocked (G12) and push
(G8) gates. While that gate is open the dispatcher launches no new Turn of
the goal for any role; Turns already running finish normally. Other goals are
unaffected.

The owner resolves the gate with one of three options:

==========================  =========  ========================================
option                      decision   effect
==========================  =========  ========================================
``raise_budget``            approve    set the budget to the first number in
                                       the gate note, else +50%; dispatching
                                       resumes and the 80%/100% alerts re-arm
``continue_without_limit``  approve    clear the budget; dispatching resumes
``stop_goal``               reject     record the owner's stop and stop the
                                       goal (``loopx goal-lifecycle``); todos
                                       are kept. Resume with ``loopx
                                       goal-lifecycle --operation resume``
==========================  =========  ========================================

``approve`` without an option means ``raise_budget``; ``reject`` and
``cancel`` mean ``stop_goal``. The gate closes through the ordinary gate
decision path (``loopx gate resolve``, ``loopx todo complete --role user
--decision-outcome`` or the dashboard ``gate.resolve``).

One crossing is one budget revision (value and ``updated_at`` of
``usage-budget.json``): the gate index remembers it, so a replayed or
restarted dispatcher never opens a second gate for the same crossing.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .file_lock import exclusive_file_lock
from .gate_threads import GATE_KIND_BUDGET_EXHAUSTED, mark_gate_closed, read_gate_index, register_gate_kind
from .usage_accounting.budget import (
    budget_status,
    read_usage_budget,
    read_usage_budget_record,
    usage_budget_path,
    write_usage_budget,
)
from .usage_accounting.ledger import goal_spend_usd

BUDGET_GATE_TEXT_PREFIX = "Usage budget exhausted: "
BUDGET_OPTION_RAISE = "raise_budget"
BUDGET_OPTION_CONTINUE = "continue_without_limit"
BUDGET_OPTION_STOP = "stop_goal"
BUDGET_GATE_OPTIONS = (BUDGET_OPTION_RAISE, BUDGET_OPTION_CONTINUE, BUDGET_OPTION_STOP)
# The decision each option records; stop also accepts cancel.
BUDGET_GATE_OPTION_DECISIONS = {
    BUDGET_OPTION_RAISE: "approve",
    BUDGET_OPTION_CONTINUE: "approve",
    BUDGET_OPTION_STOP: "reject",
}
_BUDGET_OPTION_ALLOWED_DECISIONS = {
    BUDGET_OPTION_RAISE: {"approve"},
    BUDGET_OPTION_CONTINUE: {"approve"},
    BUDGET_OPTION_STOP: {"reject", "cancel"},
}
_BUDGET_DEFAULT_OPTION = {
    None: BUDGET_OPTION_RAISE,
    "approve": BUDGET_OPTION_RAISE,
    "reject": BUDGET_OPTION_STOP,
    "cancel": BUDGET_OPTION_STOP,
}
BUDGET_RAISE_DEFAULT_FACTOR = 1.5
# Dispatcher skip reasons while a goal is held by its budget.
BUDGET_HOLD_GATE_OPEN = "budget_exhausted_gate_open"
BUDGET_HOLD_STOPPED = "budget_stopped_by_owner"
_BUDGET_ROLE_ROWS = 8
_BUDGET_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


# --- reading ---------------------------------------------------------------------


def budget_revision(record: Mapping[str, Any]) -> str:
    """One crossing identity: the budget value and when it was set."""

    return f"{float(record['budget_usd']):g}@{record.get('updated_at') or ''}"


def budget_gate_entries(runtime_root: Path, goal_id: str) -> dict[str, dict[str, Any]]:
    return {
        str(gate_id): dict(entry)
        for gate_id, entry in read_gate_index(runtime_root, goal_id)["gates"].items()
        if isinstance(entry, Mapping) and entry.get("kind") == GATE_KIND_BUDGET_EXHAUSTED
    }


def _open_user_todo_ids(registry_path: Path, goal_id: str, runtime_root: Path) -> set[str]:
    from .todos import list_goal_todos

    listed = list_goal_todos(
        registry_path=registry_path, goal_id=goal_id, role="user", status="open",
        runtime_root_arg=str(runtime_root),
    )
    return {str(item.get("todo_id")) for item in listed.get("todos") or [] if isinstance(item, Mapping)}


def open_budget_gate_id(
    entries: Mapping[str, Mapping[str, Any]], open_user_todo_ids: set[str],
) -> str | None:
    for gate_id, entry in entries.items():
        if not entry.get("closed") and gate_id in open_user_todo_ids:
            return gate_id
    return None


def budget_hold(registry_path: Path, runtime_root: Path, goal_id: str, goal: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Why the dispatcher must not launch new Turns of this goal, else None.

    * an open ``budget_exhausted`` gate while the budget is still exhausted;
    * the owner chose ``stop_goal`` and the goal is still stopped.

    A goal that never had a budget gate is never held (cheap index read).
    """

    entries = budget_gate_entries(runtime_root, goal_id)
    if not entries:
        return None
    open_gate = open_budget_gate_id(entries, _open_user_todo_ids(registry_path, goal_id, runtime_root))
    if open_gate is not None:
        status = budget_status(runtime_root, goal_id)
        if status is not None and 1.0 in status["crossed"]:
            return {"reason": BUDGET_HOLD_GATE_OPEN, "gate_todo_id": open_gate}
    from .control_plane.goals.activation import goal_is_stopped

    if not goal_is_stopped(goal):
        return None
    decided = [
        (str((entry.get("budget_outcome") or {}).get("at") or ""), gate_id)
        for gate_id, entry in entries.items()
        if isinstance(entry.get("budget_outcome"), Mapping)
    ]
    if not decided:
        return None
    _, latest = max(decided)
    if entries[latest]["budget_outcome"].get("option") == BUDGET_OPTION_STOP:
        return {"reason": BUDGET_HOLD_STOPPED, "gate_todo_id": latest}
    return None


# --- opening ---------------------------------------------------------------------


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _spend_breakdown(runtime_root: Path, goal_id: str) -> dict[str, Any]:
    from .usage_accounting.ledger import read_usage_entries
    from .usage_accounting.report import aggregate_usage

    report = aggregate_usage(read_usage_entries(runtime_root, goal_id), by="role")
    totals = report["totals"]
    return {
        "estimated_usd": round(float(totals.get("cost_estimated_usd") or 0.0), 6),
        "unpriced_turns": int(totals.get("unpriced_turns") or 0),
        "by_role": [
            {"role": row["key"], "cost_usd": row["cost_usd"], "turns": row["turns"]}
            for row in report.get("groups") or []
        ][:_BUDGET_ROLE_ROWS],
    }


def default_raised_budget(budget_usd: float) -> float:
    return round(budget_usd * BUDGET_RAISE_DEFAULT_FACTOR, 2)


def _gate_text(goal_id: str, status: Mapping[str, Any], breakdown: Mapping[str, Any]) -> str:
    spent = float(status["spent_usd"])
    budget = float(status["budget_usd"])
    estimated = float(breakdown["estimated_usd"])
    share = f"{estimated / spent * 100:.0f}%" if spent > 0 else "0%"
    roles = ", ".join(f"{row['role']} {_money(float(row['cost_usd']))}" for row in breakdown["by_role"]) or "none"
    unpriced = f"; {breakdown['unpriced_turns']} unpriced Turn(s) not counted" if breakdown["unpriced_turns"] else ""
    return (
        f"{BUDGET_GATE_TEXT_PREFIX}goal {goal_id} spent {_money(spent)} of its {_money(budget)} budget "
        f"({status['spent_ratio'] * 100:.0f}%; {_money(estimated)} or {share} of it estimated{unpriced}). "
        f"By role: {roles}. No new Turns start for this goal until you choose; running Turns finish. "
        f"Choose: raise budget (the note's amount, default +50% = {_money(default_raised_budget(budget))}), "
        "continue without limit, or stop the goal "
        f"(`loopx gate resolve --goal-id {goal_id} --todo-id <this gate> "
        "--option raise_budget|continue_without_limit|stop_goal [--note <new USD budget>]`)."
    )


def _event(runtime_root: Path, goal_id: str, event_kind: str, *, todo_id: str, status: str,
           details: Mapping[str, Any]) -> None:
    from .rollout_event_log import append_rollout_event, build_rollout_event, rollout_event_log_path

    try:
        append_rollout_event(
            rollout_event_log_path(runtime_root, goal_id),
            build_rollout_event(goal_id=goal_id, event_kind=event_kind, todo_id=todo_id, status=status,
                                details=dict(details)),
        )
    except (OSError, ValueError):
        pass  # the event log is an audit trail; the gate index already records it


def open_budget_gate(
    *, registry_path: Path, runtime_root: Path, goal_id: str,
) -> dict[str, Any]:
    """Open the goal's ``budget_exhausted`` gate once per budget crossing.

    Idempotent: an open budget gate, or any gate already opened for the
    current budget revision, is returned instead of a second one.
    """

    from .agent_registry import load_goal_from_registry, orchestrator_agent_for_goal
    from .todos import add_goal_todo

    lock = usage_budget_path(runtime_root, goal_id).with_name("usage-budget.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(lock):
        record = read_usage_budget_record(runtime_root, goal_id)
        status = budget_status(runtime_root, goal_id)
        if record is None or status is None or 1.0 not in status["crossed"]:
            return {"opened": False, "reason": "not_exhausted"}
        revision = budget_revision(record)
        entries = budget_gate_entries(runtime_root, goal_id)
        existing = open_budget_gate_id(entries, _open_user_todo_ids(registry_path, goal_id, runtime_root))
        if existing is not None:
            return {"opened": False, "reason": "budget_gate_open", "gate_todo_id": existing}
        for gate_id, entry in entries.items():
            if entry.get("budget_revision") == revision:
                return {"opened": False, "reason": "crossing_already_gated", "gate_todo_id": gate_id}
        breakdown = _spend_breakdown(runtime_root, goal_id)
        text = _gate_text(goal_id, status, breakdown)
        goal = load_goal_from_registry(Path(registry_path), goal_id)
        # The owner path (no agent id): LoopX opens this gate, like the
        # dispatcher's re-login gate (decision 17). The dispatcher holds every
        # role of the goal while it is open; the orchestrator lane also stays
        # blocked in LoopX selection.
        orchestrator = orchestrator_agent_for_goal(dict(goal or {}))
        gate = add_goal_todo(
            registry_path=Path(registry_path), goal_id=goal_id, role="user", text=text,
            task_class="user_gate", blocks_agent=orchestrator, goal_bound=orchestrator is None,
            note="Opened by the LoopX dispatcher: the goal's usage budget is exhausted (decision 41).",
            runtime_root_arg=str(runtime_root),
        )
        gate_id = str(gate.get("todo_id") or "")
        register_gate_kind(runtime_root, goal_id, gate_id, kind=GATE_KIND_BUDGET_EXHAUSTED, extra={
            "budget_revision": revision,
            "budget_usd": float(status["budget_usd"]),
            "spent_usd": float(status["spent_usd"]),
            "spent_ratio": float(status["spent_ratio"]),
            "estimated_usd": breakdown["estimated_usd"],
            "by_role": breakdown["by_role"],
            "default_raise_usd": default_raised_budget(float(status["budget_usd"])),
            "options": list(BUDGET_GATE_OPTIONS),
        })
    _event(runtime_root, goal_id, "usage_budget_exhausted", todo_id=gate_id, status="gate_opened", details={
        "gate_id": gate_id, "budget_usd": float(status["budget_usd"]), "spent_usd": float(status["spent_usd"]),
    })
    return {"opened": True, "gate_todo_id": gate_id, "gate_text": text}


# --- resolving -------------------------------------------------------------------


def budget_gate_entry(runtime_root: Path, goal_id: str, gate_todo_id: str) -> dict[str, Any] | None:
    return budget_gate_entries(runtime_root, goal_id).get(str(gate_todo_id))


def resolve_budget_gate_option(decision: str | None, option: str | None) -> str:
    """The option a decision selects; an explicit option must fit its decision."""

    if option is None:
        if decision not in _BUDGET_DEFAULT_OPTION:
            raise ValueError(f"unsupported gate decision {decision!r} for a budget_exhausted gate")
        return _BUDGET_DEFAULT_OPTION[decision]
    if option not in BUDGET_GATE_OPTION_DECISIONS:
        raise ValueError(f"budget gate option must be one of: {', '.join(BUDGET_GATE_OPTIONS)}")
    if decision is not None and decision not in _BUDGET_OPTION_ALLOWED_DECISIONS[option]:
        raise ValueError(
            f"gate option {option} records decision {BUDGET_GATE_OPTION_DECISIONS[option]}, not {decision}"
        )
    return option


def parse_budget_amount(note: str | None) -> float | None:
    """The first number in the gate note (``$75``, ``75``, ``raise to 1,200.50``), else None."""

    match = _BUDGET_AMOUNT.search(str(note or ""))
    if match is None:
        return None
    value = float(match.group(1).replace(",", ""))
    return value if math.isfinite(value) else None


def _raised_budget(
    entry: Mapping[str, Any], runtime_root: Path, goal_id: str, note: str | None, *, require_cover: bool = True,
) -> float:
    current = read_usage_budget_record(runtime_root, goal_id)
    base = float(current["budget_usd"]) if current else float(entry.get("budget_usd") or 0.0)
    amount = parse_budget_amount(note)
    target = amount if amount is not None else default_raised_budget(base)
    if not require_cover:
        # Settling: preflight already checked. Spend that grew in between
        # only makes the new budget a new crossing, which gates again.
        return target
    spent = goal_spend_usd(runtime_root, goal_id)
    if target <= spent:
        raise ValueError(
            f"a raised budget of {_money(target)} does not cover the spend {_money(spent)}; "
            "put a higher USD amount in the gate note"
        )
    return target


def budget_gate_preflight(
    *, runtime_root: Path, goal_id: str, gate_todo_id: str, decision: str | None, option: str | None,
    note: str | None = None,
) -> str | None:
    """Validate a decision on a ``budget_exhausted`` gate before it closes.

    Returns the selected option, or None for other gates. A raise that would
    not cover the spend is refused, so the gate stays open.
    """

    entry = budget_gate_entry(runtime_root, goal_id, gate_todo_id)
    if entry is None:
        return None
    selected = resolve_budget_gate_option(decision, option)
    if selected == BUDGET_OPTION_RAISE:
        _raised_budget(entry, runtime_root, goal_id, note)
    return selected


def _stop_goal(registry_path: Path, goal_id: str, gate_todo_id: str, runtime_root_arg: str | None) -> dict[str, Any]:
    from .control_plane.goals.activation_service import set_goal_activation_state

    payload = set_goal_activation_state(
        registry_path=Path(registry_path), goal_id=goal_id, state="stopped",
        reason=f"usage budget exhausted; the owner chose stop at gate {gate_todo_id}",
        runtime_root_override=runtime_root_arg, actor_kind="owner", execute=True,
    )
    return {key: payload.get(key) for key in ("ok", "changed", "written") if key in payload}


def settle_budget_gate(
    *, registry_path: Path, runtime_root: Path, goal_id: str, gate_todo_id: str, decision: str | None,
    option: str | None, note: str | None = None, runtime_root_arg: str | None = None,
) -> dict[str, Any] | None:
    """After a ``budget_exhausted`` gate closed, apply the chosen option.

    Returns None for other gates. Settling the same gate again replays its
    recorded outcome.
    """

    entry = budget_gate_entry(runtime_root, goal_id, gate_todo_id)
    if entry is None:
        return None
    if isinstance(entry.get("budget_outcome"), Mapping):
        return {"payload_key": "budget_gate", **dict(entry["budget_outcome"]), "replayed": True}
    selected = resolve_budget_gate_option(decision, option)
    outcome: dict[str, Any] = {
        "ok": True, "gate_todo_id": gate_todo_id, "decision": decision, "option": selected,
        "previous_budget_usd": read_usage_budget(runtime_root, goal_id),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        if selected == BUDGET_OPTION_RAISE:
            target = _raised_budget(entry, runtime_root, goal_id, note, require_cover=False)
            outcome["budget_usd"] = write_usage_budget(runtime_root, goal_id, target)["budget_usd"]
        elif selected == BUDGET_OPTION_CONTINUE:
            write_usage_budget(runtime_root, goal_id, None)
            outcome["budget_usd"] = None
        else:
            outcome["goal_stop"] = _stop_goal(Path(registry_path), goal_id, gate_todo_id, runtime_root_arg)
            if outcome["goal_stop"].get("ok") is False:
                outcome.update(ok=False, error="the goal could not be stopped; stop it with loopx goal-lifecycle")
            outcome["resume"] = (
                f"loopx goal-lifecycle --goal-id {goal_id} --operation resume --actor-kind owner --execute"
            )
    except (OSError, ValueError) as error:  # the gate is closed; report, never raise
        outcome.update(ok=False, error=str(error)[:400])
    mark_gate_closed(runtime_root, goal_id, gate_todo_id, decision=decision,
                     extra={"decision_option": selected, "budget_outcome": outcome})
    _event(runtime_root, goal_id, "usage_budget_decided", todo_id=gate_todo_id, status=selected, details={
        "gate_id": gate_todo_id, "option": selected, "ok": outcome["ok"],
        **({"budget_usd": outcome["budget_usd"]} if outcome.get("budget_usd") is not None else {}),
    })
    return {"payload_key": "budget_gate", **outcome}

