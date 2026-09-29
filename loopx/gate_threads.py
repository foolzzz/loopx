"""User gate discussion threads (fork slice S6, design decisions 10 and 11).

A user gate (``role=user``, ``task_class=user_gate``) carries an append-only
thread of messages ``{author: user|orchestrator, text, at}``. The thread is a
clarification channel only: the gate is still closed through the existing
decision path (``loopx todo complete --decision-outcome approve|reject|cancel``
or the dashboard ``gate.resolve`` action). The owner decides: an agent Turn
(``LOOPX_AGENT_TURN``) may reply but never decide, and every decision records
who made it (``closed_by`` in the index entry).

Durable layout under the runtime root::

    goals/<goal>/gates/<todo_id>.jsonl   append-only thread messages
    goals/<goal>/gates/index.json        per-gate summary the dispatcher watches

``awaiting`` is derived from the last message: a user reply means the gate is
``awaiting_orchestrator`` (the dispatcher wakes the orchestrator); an
orchestrator reply, or an empty thread, means ``awaiting_user``. A closed gate
reports ``closed``. Every reply also appends a ``gate_thread_reply`` rollout
event (ids and state only, never the message text) to the goal's event log.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .file_lock import exclusive_file_lock
from .history import validate_goal_id_path_segment
from .registry import atomic_write_json

GATE_THREAD_MESSAGE_SCHEMA = "loopx_gate_thread_message_v0"
GATE_THREAD_INDEX_SCHEMA = "loopx_gate_thread_index_v0"
GATE_THREAD_VIEW_SCHEMA = "loopx_gate_thread_v0"

AUTHOR_USER = "user"
AUTHOR_ORCHESTRATOR = "orchestrator"
GATE_AUTHORS = (AUTHOR_USER, AUTHOR_ORCHESTRATOR)

AWAITING_USER = "awaiting_user"
AWAITING_ORCHESTRATOR = "awaiting_orchestrator"
GATE_CLOSED = "closed"

GATE_KIND_DECISION = "decision"
GATE_KIND_PLAN_APPROVAL = "plan_approval"
# G12: opened by LoopX when the acceptor cannot review (``loopx.todo_review_blocked``).
GATE_KIND_ACCEPTOR_BLOCKED = "acceptor_blocked"
# G8: opened by LoopX when a goal's merged work is ready to push (``loopx.push_requests``).
GATE_KIND_PUSH_REQUEST = "push_request"
# Decision 41: opened by LoopX when a goal's usage budget is exhausted (``loopx.usage_budget_gate``).
GATE_KIND_BUDGET_EXHAUSTED = "budget_exhausted"
# Decision 42: opened by LoopX when a role_v1 goal's work is merged and its push resolved
# (``loopx.goal_complete_gate``).
GATE_KIND_GOAL_COMPLETE = "goal_complete"
GATE_KINDS = (
    GATE_KIND_DECISION, GATE_KIND_PLAN_APPROVAL, GATE_KIND_ACCEPTOR_BLOCKED, GATE_KIND_PUSH_REQUEST,
    GATE_KIND_BUDGET_EXHAUSTED, GATE_KIND_GOAL_COMPLETE,
)

MAX_MESSAGE_CHARS = 4000
# `plan_error` codes of a plan_approval gate whose card cannot be shown (besides PlanCardError codes).
PLAN_CARD_UNREADABLE = "plan_unreadable"
PLAN_CARD_MALFORMED = "plan_malformed"
# A settlement holds its gate's lock across its effect (a merge, a goal stop), so a
# concurrent settler of the same gate waits longer than an ordinary mutation.
GATE_SETTLEMENT_LOCK_TIMEOUT_SECONDS = 120.0
# The intent a typed settlement records before its effect runs.
GATE_SETTLEMENT_APPLYING = "applying"
GATE_SETTLEMENT_INTERRUPTED = "settlement was interrupted before its outcome was recorded; retry to recover"
# An agent Turn may not record a user-gate decision (see ``require_gate_decision_outside_agent_turn``).
GATE_DECISION_REFUSED_IN_AGENT_TURN = "gate_decision_refused_in_agent_turn"
# Where a user-gate decision was recorded: the ``closed_by.surface`` of its index entry.
GATE_DECISION_SURFACE_CLI = "cli"
GATE_DECISION_SURFACE_DASHBOARD = "dashboard"
GATE_DECISION_SURFACE_SYSTEM = "system"
GATE_DECISION_SURFACES = (GATE_DECISION_SURFACE_CLI, GATE_DECISION_SURFACE_DASHBOARD, GATE_DECISION_SURFACE_SYSTEM)


class GateThreadError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --- paths ------------------------------------------------------------------


def _safe_todo_id(todo_id: str) -> str:
    from .control_plane.todos.contract import normalize_todo_id

    normalized = normalize_todo_id(todo_id)
    if not normalized:
        raise GateThreadError("invalid_todo_id", f"invalid todo id {todo_id!r}")
    return normalized


def gates_dir(runtime_root: Path, goal_id: str) -> Path:
    return Path(runtime_root).expanduser() / "goals" / validate_goal_id_path_segment(goal_id) / "gates"


def gate_thread_path(runtime_root: Path, goal_id: str, todo_id: str) -> Path:
    return gates_dir(runtime_root, goal_id) / f"{_safe_todo_id(todo_id)}.jsonl"


def gate_index_path(runtime_root: Path, goal_id: str) -> Path:
    return gates_dir(runtime_root, goal_id) / "index.json"


def gate_settlement_lock_path(runtime_root: Path, goal_id: str, todo_id: str) -> Path:
    """The path whose sibling lock serialises one gate's typed settlement."""

    return gates_dir(runtime_root, goal_id) / f"{_safe_todo_id(todo_id)}.settle"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --- raw storage ------------------------------------------------------------


def read_gate_thread(runtime_root: Path, goal_id: str, todo_id: str) -> list[dict[str, Any]]:
    """Return the thread's messages in append order (torn lines are skipped)."""

    path = gate_thread_path(runtime_root, goal_id, todo_id)
    if not path.exists():
        return []
    messages: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("schema_version") == GATE_THREAD_MESSAGE_SCHEMA:
            messages.append(row)
    return messages


def read_gate_index(runtime_root: Path, goal_id: str) -> dict[str, Any]:
    path = gate_index_path(runtime_root, goal_id)
    if not path.exists():
        return {"schema_version": GATE_THREAD_INDEX_SCHEMA, "goal_id": goal_id, "gates": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    gates = payload.get("gates") if isinstance(payload, dict) else None
    return {
        "schema_version": GATE_THREAD_INDEX_SCHEMA,
        "goal_id": goal_id,
        "gates": dict(gates) if isinstance(gates, dict) else {},
    }


def awaiting_for(messages: list[Mapping[str, Any]], *, closed: bool = False) -> str:
    if closed:
        return GATE_CLOSED
    if messages and messages[-1].get("author") == AUTHOR_USER:
        return AWAITING_ORCHESTRATOR
    return AWAITING_USER


def _index_entry(
    previous: Mapping[str, Any] | None, messages: list[Mapping[str, Any]], *, closed: bool,
) -> dict[str, Any]:
    entry = dict(previous or {})
    entry.setdefault("kind", GATE_KIND_DECISION)
    entry["message_count"] = len(messages)
    entry["awaiting"] = awaiting_for(messages, closed=closed)
    entry["closed"] = closed
    if messages:
        entry["last_author"] = messages[-1].get("author")
        entry["last_at"] = messages[-1].get("at")
        entry["last_message_id"] = messages[-1].get("message_id")
    entry["updated_at"] = _now()
    return entry


def _write_index_entry(
    runtime_root: Path, goal_id: str, todo_id: str, update: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge ``update`` into one gate's index entry. Caller holds the lock."""

    index = read_gate_index(runtime_root, goal_id)
    entry = {**dict(index["gates"].get(todo_id) or {}), **dict(update)}
    index["gates"][todo_id] = entry
    atomic_write_json(gate_index_path(runtime_root, goal_id), index)
    return entry


def register_gate_kind(
    runtime_root: Path, goal_id: str, todo_id: str, *, kind: str, plan_id: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a gate's kind (and plan link or other kind fields) so readers can render it."""

    if kind not in GATE_KINDS:
        raise GateThreadError("invalid_gate_kind", f"gate kind must be one of: {', '.join(GATE_KINDS)}")
    todo_id = _safe_todo_id(todo_id)
    index_path = gate_index_path(runtime_root, goal_id)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(index_path):
        messages = read_gate_thread(runtime_root, goal_id, todo_id)
        previous = read_gate_index(runtime_root, goal_id)["gates"].get(todo_id)
        entry = _index_entry(previous, messages, closed=bool((previous or {}).get("closed")))
        entry["kind"] = kind
        if plan_id:
            entry["plan_id"] = plan_id
        entry.update(dict(extra or {}))
        return _write_index_entry(runtime_root, goal_id, todo_id, entry)


def mark_gate_closed(
    runtime_root: Path, goal_id: str, todo_id: str, *, decision: str | None,
    extra: Mapping[str, Any] | None = None, closed_by: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a gate's index entry closed (creating the entry). A recorded ``closed_by`` is kept."""

    todo_id = _safe_todo_id(todo_id)
    index_path = gate_index_path(runtime_root, goal_id)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(index_path):
        messages = read_gate_thread(runtime_root, goal_id, todo_id)
        previous = read_gate_index(runtime_root, goal_id)["gates"].get(todo_id)
        entry = _index_entry(previous, messages, closed=True)
        if decision:
            entry["decision_outcome"] = decision
        if closed_by is not None and not entry.get("closed_by"):
            entry["closed_by"] = dict(closed_by)
        entry.update(dict(extra or {}))
        return _write_index_entry(runtime_root, goal_id, todo_id, entry)


def record_gate_decision(
    runtime_root: Path, goal_id: str, todo_id: str, *, decision: str, surface: str, actor: str | None,
) -> dict[str, Any]:
    """Mark a decided user gate closed and record who decided it.

    ``closed_by`` is ``{surface, actor, agent_turn, at}``: ``cli``,
    ``dashboard`` or ``system``; the lifecycle actor, or ``owner`` without
    one; the agent-Turn marker of the recording process (null outside an
    agent Turn); and when. A replayed or re-settled decision keeps the first
    record.
    """

    from .control_plane.agents.agent_turn import agent_turn_marker

    if surface not in GATE_DECISION_SURFACES:
        raise GateThreadError(
            "invalid_gate_decision_surface", f"gate decision surface must be one of: {', '.join(GATE_DECISION_SURFACES)}"
        )
    return mark_gate_closed(runtime_root, goal_id, todo_id, decision=decision, closed_by={
        "surface": surface, "actor": actor or "owner", "agent_turn": agent_turn_marker(), "at": _now(),
    })


def require_gate_decision_outside_agent_turn(*, goal_id: str, todo_id: str) -> None:
    """Refuse a user-gate decision (approve, reject, cancel or an option) inside an agent Turn.

    The owner decides user gates; an agent asks or answers in the gate thread.
    Checked before anything is written. A guardrail against accidental
    self-approval, not a security boundary: see
    :mod:`loopx.control_plane.agents.agent_turn`.
    """

    from .control_plane.agents.agent_turn import AGENT_TURN_ENV_VAR, agent_turn_marker

    agent = agent_turn_marker()
    if agent is None:
        return
    raise GateThreadError(
        GATE_DECISION_REFUSED_IN_AGENT_TURN,
        f"the owner decides user gates; this agent Turn ({AGENT_TURN_ENV_VAR}={agent}) cannot approve, reject or "
        f"cancel gate {todo_id!r}. Ask or answer in its thread instead: loopx gate reply --goal-id {goal_id} "
        f"--todo-id {todo_id} --as orchestrator --agent-id <orchestrator> --text '...', and wait for the owner.",
    )


def run_gate_settlement(
    runtime_root: Path, goal_id: str, todo_id: str, *, decision: str | None, option: str,
    outcome_key: str, apply: Callable[[str | None, str, Mapping[str, Any]], Mapping[str, Any]],
    pin: Callable[[str | None, str], Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Claim, apply and record one typed gate settlement in one critical section.

    Under the gate's exclusive settlement lock: an outcome recorded under
    ``outcome_key`` that did not fail replays, and nothing runs. Otherwise the
    first attempt writes an intent before the effect: the decision, the option
    and the inputs ``pin`` computes (``pinned``), recorded as a failed
    ``applying`` outcome. A crash after the effect therefore leaves a durable
    winner. Then ``apply(decision, option, prior)`` runs the effect, where
    ``prior`` is that intent or the failed outcome being retried, and its
    outcome is recorded before the lock is released. A recorded failed outcome
    is not final: the next settlement retries it with the recorded decision,
    option and ``pinned`` inputs, so a retry repeats the first choice with the
    same inputs. Returns the outcome and whether this call ran the effect.

    Lock order: this per-gate lock is taken first and held across the effect.
    The effect then takes its own locks (the budget file, the goal lifecycle,
    todo writes, the git workspace), and the gate index lock is taken last to
    record. No effect settles a user gate, so settlement locks never nest and
    no lock the effect holds is ever waited on while taking this one.
    """

    todo_id = _safe_todo_id(todo_id)
    lock_path = gate_settlement_lock_path(runtime_root, goal_id, todo_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(lock_path, timeout_seconds=GATE_SETTLEMENT_LOCK_TIMEOUT_SECONDS,
                             operation="settle_gate"):
        entry = read_gate_index(runtime_root, goal_id)["gates"].get(todo_id) or {}
        recorded = entry.get(outcome_key)
        if isinstance(recorded, Mapping):
            if recorded.get("ok") is not False:
                return dict(recorded), False
            decision = entry.get("decision_outcome") or decision
            option = str(entry.get("decision_option") or option)
            prior = dict(recorded)
        else:
            prior = {"ok": False, "state": GATE_SETTLEMENT_APPLYING, "error": GATE_SETTLEMENT_INTERRUPTED}
            pinned = dict(pin(decision, option)) if pin is not None else {}
            if pinned:
                prior["pinned"] = pinned
            mark_gate_closed(runtime_root, goal_id, todo_id, decision=decision,
                             extra={"decision_option": option, outcome_key: prior})
        outcome = dict(apply(decision, option, prior))
        if outcome.get("ok") is False and "pinned" in prior:
            outcome.setdefault("pinned", prior["pinned"])
        mark_gate_closed(runtime_root, goal_id, todo_id, decision=decision,
                         extra={"decision_option": option, outcome_key: outcome})
        return outcome, True


def append_gate_message(
    runtime_root: Path, goal_id: str, todo_id: str, *, author: str, text: str,
    agent_id: str | None = None, at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Append one message and refresh the index atomically under one lock."""

    if author not in GATE_AUTHORS:
        raise GateThreadError("invalid_author", f"author must be one of: {', '.join(GATE_AUTHORS)}")
    body = str(text or "").strip()
    if not body:
        raise GateThreadError("empty_message", "reply text must not be empty")
    if len(body) > MAX_MESSAGE_CHARS:
        raise GateThreadError(
            "message_too_long", f"reply text must be at most {MAX_MESSAGE_CHARS} characters"
        )
    todo_id = _safe_todo_id(todo_id)
    thread = gate_thread_path(runtime_root, goal_id, todo_id)
    index_path = gate_index_path(runtime_root, goal_id)
    thread.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(index_path):
        messages = read_gate_thread(runtime_root, goal_id, todo_id)
        seq = len(messages) + 1
        stamp = at or _now()
        message = {
            "schema_version": GATE_THREAD_MESSAGE_SCHEMA,
            "goal_id": goal_id,
            "todo_id": todo_id,
            "seq": seq,
            "author": author,
            "agent_id": agent_id,
            "text": body,
            "at": stamp,
        }
        message["message_id"] = "gmsg_" + hashlib.sha256(
            json.dumps(message, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        encoded = json.dumps(message, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
        with thread.open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() > 0:
                handle.seek(-1, 2)
                if handle.read(1) != b"\n":
                    handle.write(b"\n")
            handle.write(encoded)
            handle.flush()
        messages.append(message)
        previous = read_gate_index(runtime_root, goal_id)["gates"].get(todo_id)
        entry = _write_index_entry(
            runtime_root, goal_id, todo_id, _index_entry(previous, messages, closed=False)
        )
    return message, entry


# --- goal / gate lookups ----------------------------------------------------


def _goal(registry_path: Path, goal_id: str) -> dict[str, Any]:
    from .agent_registry import load_goal_from_registry

    goal = load_goal_from_registry(Path(registry_path), goal_id)
    if goal is None:
        raise GateThreadError("goal_not_registered", f"goal {goal_id!r} is not in the registry")
    return goal


def _gate_todo(registry_path: Path, goal_id: str, todo_id: str, runtime_root_arg: str | None) -> dict[str, Any]:
    from .todos import list_goal_todos

    payload = list_goal_todos(
        registry_path=Path(registry_path), goal_id=goal_id, runtime_root_arg=runtime_root_arg,
    )
    todo = next(
        (row for row in payload.get("todos") or [] if isinstance(row, dict) and row.get("todo_id") == todo_id),
        None,
    )
    if todo is None:
        raise GateThreadError("gate_not_found", f"gate todo {todo_id!r} was not found in goal {goal_id!r}")
    if todo.get("role") != "user" or todo.get("task_class") != "user_gate":
        raise GateThreadError("not_a_user_gate", f"todo {todo_id!r} is not a user gate (role=user, task_class=user_gate)")
    return dict(todo)


def _gate_is_open(todo: Mapping[str, Any]) -> bool:
    return str(todo.get("status") or "open") in {"open", "blocked"}


def require_goal_orchestrator(goal: Mapping[str, Any], agent_id: str | None) -> str:
    from .agent_registry import orchestrator_agent_for_goal

    orchestrator = orchestrator_agent_for_goal(dict(goal))
    if orchestrator is None:
        raise GateThreadError(
            "no_orchestrator",
            "this goal has no role_v1 orchestrator; register one with "
            "`loopx configure-goal --agent-role AGENT=orchestrator --execute`",
        )
    if not agent_id:
        raise GateThreadError("agent_id_required", "orchestrator actions require --agent-id")
    if agent_id != orchestrator:
        raise GateThreadError(
            "not_orchestrator",
            f"agent {agent_id!r} is not the orchestrator of goal {goal.get('id')!r} "
            f"(orchestrator: {orchestrator!r})",
        )
    return orchestrator


# --- decision 11: only the orchestrator opens user gates --------------------


def require_user_gate_author(
    *, registry_path: Path, goal_id: str, actor_agent_id: str | None, surface: str = "todo add",
) -> None:
    """Refuse user-todo creation by developer/acceptor agents under role_v1.

    The owner (no agent id) may always open gates; peer_v1 goals and agents
    without a registered role keep the previous behaviour.
    """

    if not actor_agent_id:
        return
    from .agent_registry import agent_role_for_goal, load_goal_from_registry
    from .control_plane.agents.runtime_model import (
        AGENT_ROLE_ORCHESTRATOR,
        AgentRuntimeModel,
        agent_runtime_model_for_goal,
    )

    goal = load_goal_from_registry(Path(registry_path), goal_id)
    if goal is None:
        return
    try:
        if agent_runtime_model_for_goal(goal) != AgentRuntimeModel.ROLE_V1:
            return
    except ValueError:
        return
    role = agent_role_for_goal(goal, actor_agent_id)
    if role is None or role == AGENT_ROLE_ORCHESTRATOR:
        return
    raise GateThreadError(
        "gate_requires_orchestrator",
        f"under role_v1 only the goal orchestrator opens user gates; {role} agent "
        f"{actor_agent_id!r} cannot create one via {surface}. Raise a blocker to the "
        f"orchestrator instead: loopx todo add --goal-id {goal_id} --role agent "
        "--task-class blocker --text '<question or blocker>'",
    )


# --- public operations ------------------------------------------------------


def _append_reply_event(runtime_root: Path, goal_id: str, message: Mapping[str, Any], awaiting: str) -> None:
    from .rollout_event_log import append_rollout_event, build_rollout_event, rollout_event_log_path

    event = build_rollout_event(
        goal_id=goal_id,
        event_kind="gate_thread_reply",
        agent_id=message.get("agent_id") or None,
        todo_id=str(message["todo_id"]),
        agent_role=str(message["author"]),
        gate_id=str(message["todo_id"]),
        to_state=awaiting,
        status=awaiting,
        details={"seq": int(message["seq"]), "message_id": str(message["message_id"])},
        recorded_at=str(message["at"]),
    )
    append_rollout_event(rollout_event_log_path(runtime_root, goal_id), event)


def reply_to_gate(
    *, registry_path: Path, runtime_root: Path, goal_id: str, todo_id: str, text: str,
    author: str = AUTHOR_USER, agent_id: str | None = None, runtime_root_arg: str | None = None,
) -> dict[str, Any]:
    """Append a reply to an open user gate's thread.

    User replies are owner actions (no agent id). Orchestrator replies must
    come from the goal's orchestrator.
    """

    goal = _goal(registry_path, goal_id)
    todo_id = _safe_todo_id(todo_id)
    if author == AUTHOR_ORCHESTRATOR:
        require_goal_orchestrator(goal, agent_id)
    elif author == AUTHOR_USER:
        if agent_id:
            raise GateThreadError(
                "user_reply_has_agent",
                "user replies are owner actions; omit --agent-id or reply --as orchestrator",
            )
    else:
        raise GateThreadError("invalid_author", f"author must be one of: {', '.join(GATE_AUTHORS)}")
    gate = _gate_todo(registry_path, goal_id, todo_id, runtime_root_arg)
    if not _gate_is_open(gate):
        raise GateThreadError(
            "gate_closed", f"gate {todo_id!r} is {gate.get('status')}; the thread is read-only"
        )
    message, entry = append_gate_message(
        runtime_root, goal_id, todo_id, author=author, text=text, agent_id=agent_id,
    )
    _append_reply_event(runtime_root, goal_id, message, str(entry["awaiting"]))
    return {
        "ok": True,
        "schema_version": GATE_THREAD_VIEW_SCHEMA,
        "goal_id": goal_id,
        "todo_id": todo_id,
        "message": message,
        "awaiting": entry["awaiting"],
        "message_count": entry["message_count"],
    }


GATE_RESOLVE_AUTHORITY_REASON = "owner decision via loopx gate resolve"


def resolve_gate(
    *, registry_path: Path, goal_id: str, todo_id: str, decision: str | None = None,
    option: str | None = None, note: str | None = None, agent_id: str | None = None,
    runtime_root_arg: str | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Close a user gate with an owner decision (``loopx gate resolve``).

    The same path as ``loopx todo complete --role user --decision-outcome`` and
    the dashboard ``gate.resolve``; the lifecycle actor is the agent the gate
    blocks. ``option`` picks one of an acceptor-blocked (G12),
    budget_exhausted (decision 41) or goal_complete (decision 42) gate's
    options and implies its decision. Refused inside an agent Turn; the
    decision is recorded as made on the ``cli`` surface.
    """

    from .todos import complete_goal_todo

    todo_id = _safe_todo_id(todo_id)
    gate = _gate_todo(registry_path, goal_id, todo_id, runtime_root_arg)
    if not _gate_is_open(gate):
        raise GateThreadError("gate_closed", f"gate {todo_id!r} is already {gate.get('status')}")
    if option is not None and decision is None:
        from .goal_complete_gate import GOAL_COMPLETE_OPTION_DECISIONS
        from .todo_review_blocked import REVIEW_GATE_OPTION_DECISIONS
        from .usage_budget_gate import BUDGET_GATE_OPTION_DECISIONS

        decision = {
            **REVIEW_GATE_OPTION_DECISIONS, **BUDGET_GATE_OPTION_DECISIONS, **GOAL_COMPLETE_OPTION_DECISIONS,
        }.get(option)
    if decision not in {"approve", "reject", "cancel"}:
        raise GateThreadError("decision_required", "gate resolve requires --decision approve|reject|cancel or --option")
    bound = gate.get("bound_agent") or gate.get("blocks_agent")
    if agent_id and bound and agent_id != bound:
        raise GateThreadError("gate_actor_mismatch", f"gate {todo_id!r} belongs to agent {bound!r}, not {agent_id!r}")
    return complete_goal_todo(
        registry_path=Path(registry_path), goal_id=goal_id, todo_id=todo_id, role="user",
        decision_outcome=decision, gate_option=option, note=note, no_followup=True,
        agent_id=agent_id or bound or None, authority_reason=GATE_RESOLVE_AUTHORITY_REASON,
        runtime_root_arg=runtime_root_arg, dry_run=dry_run,
    )


def gate_view(
    *, registry_path: Path, runtime_root: Path, goal_id: str, todo_id: str,
    runtime_root_arg: str | None = None,
) -> dict[str, Any]:
    """Thread plus status for one user gate."""

    _goal(registry_path, goal_id)
    todo_id = _safe_todo_id(todo_id)
    gate = _gate_todo(registry_path, goal_id, todo_id, runtime_root_arg)
    messages = read_gate_thread(runtime_root, goal_id, todo_id)
    entry = read_gate_index(runtime_root, goal_id)["gates"].get(todo_id) or {}
    closed = not _gate_is_open(gate)
    view: dict[str, Any] = {
        "ok": True,
        "schema_version": GATE_THREAD_VIEW_SCHEMA,
        "goal_id": goal_id,
        "todo_id": todo_id,
        "text": gate.get("text"),
        "status": gate.get("status"),
        "decision_outcome": gate.get("decision_outcome"),
        "kind": entry.get("kind") or GATE_KIND_DECISION,
        "awaiting": awaiting_for(messages, closed=closed),
        "bound_agent": gate.get("bound_agent"),
        "unblocks_todo_id": gate.get("unblocks_todo_id"),
        "messages": [
            {key: message.get(key) for key in ("seq", "message_id", "author", "agent_id", "text", "at")}
            for message in messages
        ],
    }
    if entry.get("plan_id"):
        view["plan_id"] = entry["plan_id"]
        from .plan_cards import PlanCardError, plan_card_view, read_plan
        from .plan_criteria_changes import CRITERIA_CHANGES_MALFORMED, criteria_changes_view

        # A missing, unreadable or malformed card leaves the thread readable and is
        # reported by code, never shown as an empty plan or as "no criteria changes".
        record: dict[str, Any] | None = None
        try:
            record = read_plan(runtime_root, goal_id, str(entry["plan_id"]))
        except PlanCardError as error:
            view["plan_error"] = error.code
        except (OSError, ValueError):
            view["plan_error"] = PLAN_CARD_UNREADABLE
        if record is not None:
            card = plan_card_view(record)
            if card:  # what approving applies: summary and each todo's contract
                view["plan"] = card
            else:
                view["plan_error"] = PLAN_CARD_MALFORMED
            changes = criteria_changes_view(record)
            if changes is None:
                view["criteria_changes_error"] = CRITERIA_CHANGES_MALFORMED
            elif changes:  # decision 40: old and new criteria side by side
                view["criteria_changes"] = changes
    for key in ("review_todo_id", "acceptor_agent", "options", "decision_option",
                "push_reason", "push_repos", "previous_errors", "push_outcome",
                "budget_usd", "spent_usd", "spent_ratio", "estimated_usd", "by_role", "default_raise_usd",
                "budget_outcome", "completion_key", "completion_repos", "completion_todos", "completion_usage",
                "follow_ups", "completion_outcome", "closed_by"):
        if entry.get(key):
            view[key] = entry[key]
    return view


def list_gates(
    *, registry_path: Path, runtime_root: Path, goal_id: str, awaiting: str | None = None,
    runtime_root_arg: str | None = None,
) -> dict[str, Any]:
    """Open user gates with their thread state (the dispatcher's read model)."""

    from .todos import list_goal_todos

    _goal(registry_path, goal_id)
    payload = list_goal_todos(
        registry_path=Path(registry_path), goal_id=goal_id, runtime_root_arg=runtime_root_arg,
    )
    index = read_gate_index(runtime_root, goal_id)["gates"]
    rows: list[dict[str, Any]] = []
    for todo in payload.get("todos") or []:
        if not isinstance(todo, dict) or todo.get("role") != "user" or todo.get("task_class") != "user_gate":
            continue
        if not _gate_is_open(todo):
            continue
        todo_id = str(todo.get("todo_id"))
        messages = read_gate_thread(runtime_root, goal_id, todo_id)
        entry = index.get(todo_id) or {}
        row = {
            "todo_id": todo_id,
            "text": todo.get("text"),
            "kind": entry.get("kind") or GATE_KIND_DECISION,
            "awaiting": awaiting_for(messages),
            "message_count": len(messages),
            "last_at": messages[-1].get("at") if messages else None,
        }
        if entry.get("plan_id"):
            row["plan_id"] = entry["plan_id"]
        if entry.get("options"):
            row["options"] = entry["options"]
        if awaiting and row["awaiting"] != awaiting:
            continue
        rows.append(row)
    return {"ok": True, "schema_version": "loopx_gate_list_v0", "goal_id": goal_id, "gates": rows}


def gates_awaiting_orchestrator(runtime_root: Path, goal_id: str) -> list[str]:
    """Cheap index-only read for the dispatcher (S4): gates whose last reply is the user's."""

    return sorted(
        todo_id
        for todo_id, entry in read_gate_index(runtime_root, goal_id)["gates"].items()
        if isinstance(entry, dict) and entry.get("awaiting") == AWAITING_ORCHESTRATOR
    )


def _render_plan_card_markdown(card: Mapping[str, Any]) -> list[str]:
    """The bounded plan card (``plan_card_view``) a ``gate show`` reviewer decides on."""

    lines = ["", f"## Plan `{card.get('plan_id')}`: {card.get('title')} "
                 f"(revision {card.get('revision')}, {card.get('status')})"]
    if card.get("summary"):
        lines += ["", str(card["summary"])]
    todos: Iterable[Mapping[str, Any]] = card.get("todos") or []
    if todos:
        lines.append("")
    for index, todo in enumerate(todos, start=1):
        facts = [str(todo.get("required_role") or "developer")]
        if todo.get("task_repositories"):
            facts.append("repos: " + ", ".join(todo["task_repositories"]))
        if todo.get("depends_on"):
            facts.append("after: " + ", ".join(todo["depends_on"]))
        lines.append(f"{index}. `{todo.get('key')}` {todo.get('text')} ({'; '.join(facts)})")
        if todo.get("acceptance"):
            lines.append(f"   - acceptance: {todo['acceptance']}")
        if todo.get("validation_command"):
            lines.append(f"   - validation: `{todo['validation_command']}`")
    return lines


def render_gate_markdown(payload: Mapping[str, Any]) -> str:
    if not payload.get("ok"):
        return f"gate: error: {payload.get('error')}\n"
    if "gates" in payload:
        lines = [f"# Open gates for {payload.get('goal_id')}", ""]
        for row in payload.get("gates") or []:
            lines.append(
                f"- `{row['todo_id']}` [{row['kind']}] {row['awaiting']} "
                f"({row['message_count']} messages): {row.get('text')}"
            )
        if len(lines) == 2:
            lines.append("- none")
        return "\n".join(lines) + "\n"
    if "messages" not in payload and ("review_gate" in payload or "plan_card" in payload):
        settled = payload.get("review_gate") or payload.get("plan_card") or {}
        return (
            f"Resolved gate `{payload.get('todo_id')}`: {payload.get('decision_outcome')}"
            + (f" ({settled.get('option')})" if settled.get("option") else "")
            + (f"; error: {settled.get('error')}" if settled.get("error") else "") + "\n"
        )
    if "message" in payload and "messages" not in payload:
        message = payload["message"]
        return (
            f"Replied to gate `{payload['todo_id']}` as {message['author']} "
            f"(#{message['seq']}); now {payload['awaiting']}.\n"
        )
    lines = [
        f"# Gate `{payload.get('todo_id')}` ({payload.get('kind')})",
        "",
        f"- text: {payload.get('text')}",
        f"- status: {payload.get('status')}"
        + (f" ({payload.get('decision_outcome')})" if payload.get("decision_outcome") else ""),
        f"- awaiting: {payload.get('awaiting')}",
    ]
    closed_by = payload.get("closed_by")
    if isinstance(closed_by, Mapping):
        lines.append(
            f"- closed by: {closed_by.get('surface')} (actor {closed_by.get('actor')}"
            + (f", agent Turn {closed_by['agent_turn']}" if closed_by.get("agent_turn") else "")
            + f") at {closed_by.get('at')}"
        )
    if payload.get("plan_id"):
        lines.append(f"- plan: `{payload['plan_id']}` (see `loopx plan show`)")
    if payload.get("plan_error"):
        lines.append(f"- plan card unavailable ({payload['plan_error']}); review it with `loopx plan show` before deciding")
    if payload.get("plan"):
        lines += _render_plan_card_markdown(payload["plan"])
    if payload.get("criteria_changes_error"):
        from .plan_criteria_changes import render_criteria_changes_unavailable_markdown

        lines += render_criteria_changes_unavailable_markdown(str(payload["criteria_changes_error"]))
    elif payload.get("criteria_changes"):
        from .plan_criteria_changes import render_criteria_changes_markdown

        lines += render_criteria_changes_markdown(payload["criteria_changes"])
    lines += ["", "## Thread", ""]
    messages: Iterable[Mapping[str, Any]] = payload.get("messages") or []
    any_message = False
    for message in messages:
        any_message = True
        who = message.get("author")
        if message.get("agent_id"):
            who = f"{who} ({message['agent_id']})"
        lines.append(f"{message.get('seq')}. **{who}** at {message.get('at')}:")
        lines.extend(f"   {line}" for line in str(message.get("text") or "").splitlines())
    if not any_message:
        lines.append("_No messages yet._")
    return "\n".join(lines) + "\n"
