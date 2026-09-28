"""One executing bounded Turn per Turn lane.

A Turn lane is one agent working one goal. Two executing Turns for the same
lane would run two executors at once: each invokes its own host, writes its own
delivery, and spends its own quota slot, so the lane ends up with two answers to
one bounded question. LoopX therefore admits exactly one *executing* Turn per
lane and refuses the second with a typed, retryable refusal naming the holder.

Fork role_v1 (design-v0 decision 32): a developer or acceptor Turn that works a
selected todo runs in a *todo lane* keyed by (goal, todo) instead. One agent may
then run several todos of one goal in parallel (bounded by its
``max_concurrency`` in the dispatcher), while any one todo still has at most one
executing Turn, whichever agent runs it. The orchestrator, peer_v1 goals and
Turns without a selected todo keep the (agent, goal) lane. The caller that knows
the goal's model and the agent's role picks the scope
(:func:`turn_lane_todo_scope`); this module only fences it.

The fence is a kernel lock held by the executing process, so a crashed or killed
Turn releases the lane instead of leaving a stale claim no later Turn can enter.
Previews and other non-executing decisions never take it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from ...file_lock import lock_holder_path, try_exclusive_file_lock

# Typed refusal for a lane whose single executor is already busy. The reason is
# a fact about this lane, so a caller can retry it unchanged once it clears.
TURN_LANE_IN_FLIGHT = "turn_lane_in_flight"
# The operator-reachable exit: wait for the named Turn to settle, then retry.
REMEDY_WAIT_FOR_IN_FLIGHT_TURN = "wait_for_in_flight_turn"
TURN_LANE_OPERATION = "loopx_turn_lane"
TURN_LANE_DIR_NAME = ".lanes"
TURN_LANE_UNATTRIBUTED_AGENT = "unattributed"
# Lane scopes: the default (agent, goal) lane and the role_v1 (goal, todo) lane.
TURN_LANE_SCOPE_AGENT = "agent"
TURN_LANE_SCOPE_TODO = "todo"
# role_v1 roles whose Turns run in a todo lane; the orchestrator stays serial.
TURN_LANE_TODO_SCOPED_ROLES = frozenset({"developer", "acceptor"})
# Public-safe holder fields only: the lock record also carries a lock id, a
# policy name, and the private lock path, which never leave this process. The
# host is projected because two hosts can share one runtime root: a refusal on
# the second host must not print a pid that cannot exist there.
TURN_LANE_HOLDER_TEXT_FIELDS = ("agent_id", "operation", "acquired_at", "host")
# A refusal taken here stops before the journal, the host, and quota, so the
# payload reports the same effect shape an executing Turn does -- all false.
TURN_LANE_NO_EFFECTS: dict[str, bool] = {
    "host_invoked": False,
    "state_written": False,
    "quota_spent": False,
    "scheduler_acknowledged": False,
}
_LANE_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
TURN_LANE_EXECUTION_PAYLOAD = Callable[..., dict[str, Any]]
TURN_LANE_TURN_RUNNER = Callable[..., dict[str, Any]]


def turn_lane_agent_id(plan: Mapping[str, Any]) -> str:
    """Return the agent whose lane this Turn executes in.

    The Turn envelope owns the agent identity. A plan without one still gets a
    lane rather than no fence at all, because two unattributed Turns on one goal
    are exactly the overlap this module exists to refuse.
    """

    envelope = plan.get("turn_envelope")
    if isinstance(envelope, Mapping):
        agent_id = str(envelope.get("agent_id") or "").strip()
        if agent_id:
            return agent_id
    return TURN_LANE_UNATTRIBUTED_AGENT


def turn_lane_todo_scope(
    *,
    role_v1: bool,
    agent_role: str | None,
    todo_id: str | None,
) -> str | None:
    """Return the todo whose lane a Turn runs in, or ``None`` for the agent lane.

    Only a role_v1 developer or acceptor Turn with a selected todo is todo
    scoped. The orchestrator stays strictly serial per goal (decision 19), and
    peer_v1 goals keep the (agent, goal) lane unchanged.
    """

    todo = str(todo_id or "").strip()
    if not role_v1 or not todo or agent_role not in TURN_LANE_TODO_SCOPED_ROLES:
        return None
    return todo


def turn_lane_target(
    *,
    runtime_root: Path,
    goal_id: str,
    plan: Mapping[str, Any],
    todo_id: str | None = None,
) -> Path:
    """Return the lock target one lane's executing Turn holds.

    With ``todo_id`` the lane is the (goal, todo) lane: it does not depend on
    the agent, so two agents can never execute the same todo at once either.
    """

    lanes = Path(runtime_root) / "goals" / goal_id / "turns" / TURN_LANE_DIR_NAME
    todo = str(todo_id or "").strip()
    if todo:
        readable = _LANE_NAME_UNSAFE.sub("_", todo)[:64] or "todo"
        digest = hashlib.sha256(
            f"{goal_id}\0{TURN_LANE_SCOPE_TODO}\0{todo}".encode()
        ).hexdigest()[:12]
        return lanes / f"{TURN_LANE_SCOPE_TODO}-{readable}-{digest}.lane"
    agent_id = turn_lane_agent_id(plan)
    readable = _LANE_NAME_UNSAFE.sub("_", agent_id)[:64] or TURN_LANE_UNATTRIBUTED_AGENT
    digest = hashlib.sha256(f"{goal_id}\0{agent_id}".encode()).hexdigest()[:12]
    return lanes / f"{readable}-{digest}.lane"


@contextmanager
def turn_lane_singleflight(
    *,
    runtime_root: Path,
    goal_id: str,
    plan: Mapping[str, Any],
    todo_id: str | None = None,
) -> Iterator[Path | None]:
    """Hold one lane for one executing Turn.

    ``None`` means another process already owns this lane's executor, which the
    caller reports as the typed refusal instead of running a second executor.
    """

    target = turn_lane_target(
        runtime_root=runtime_root, goal_id=goal_id, plan=plan, todo_id=todo_id
    )
    with try_exclusive_file_lock(
        target,
        agent_id=turn_lane_agent_id(plan),
        operation=TURN_LANE_OPERATION,
    ) as lock_path:
        yield lock_path


def turn_lane_holder_readback(target: Path) -> dict[str, Any]:
    """Return the public-safe identity of the Turn holding one lane, else ``{}``.

    Only names, a timestamp, a machine name and a process id are projected: the
    holder record's private lock path and lock id stay out, so a refusal can say
    who is running where without publishing where a machine keeps its runtime
    state. The machine name is what makes the projected pid actionable when two
    hosts share one runtime root.
    """

    try:
        record = json.loads(lock_holder_path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(record, Mapping):
        return {}
    projection: dict[str, Any] = {}
    for field in TURN_LANE_HOLDER_TEXT_FIELDS:
        value = record.get(field)
        if isinstance(value, str) and value:
            projection[field] = value
    pid = record.get("pid")
    if isinstance(pid, int):
        projection["pid"] = pid
    return projection


def turn_lane_in_flight_record(
    plan: Mapping[str, Any],
    *,
    holder: Mapping[str, Any],
    todo_id: str | None = None,
) -> dict[str, Any]:
    """Return the fail-closed result record for a lane already executing a Turn.

    The planned host is named as readback, never as an invocation: this refusal
    stops before the journal, the host, and quota, so it must not claim a host
    identity it did not build.
    """

    planned_host = plan.get("host") if isinstance(plan.get("host"), dict) else {}
    return {
        "status": "unavailable",
        "host": {
            "executable": "not_invoked",
            "kind": str(planned_host.get("kind") or ""),
        },
        "reason": TURN_LANE_IN_FLIGHT,
        "remediation": [REMEDY_WAIT_FOR_IN_FLIGHT_TURN],
        **({"in_flight": dict(holder)} if holder else {}),
        "turn_lane": turn_lane_scope_projection(todo_id),
    }


def turn_lane_scope_projection(todo_id: str | None) -> dict[str, str]:
    """Name which lane a refusal is about: the agent lane or one todo lane."""

    todo = str(todo_id or "").strip()
    if todo:
        return {"scope": TURN_LANE_SCOPE_TODO, "todo_id": todo}
    return {"scope": TURN_LANE_SCOPE_AGENT}


def turn_lane_in_flight_projection(journal: Mapping[str, Any]) -> dict[str, Any]:
    """Return the ``in_flight`` holder entry of a lane refusal, or nothing.

    The holder identity is this module's own readback, so the entry is projected
    here and the executor only spreads it into the execution payload.
    """

    holder = journal.get("in_flight")
    projection: dict[str, Any] = (
        {"in_flight": dict(holder)} if isinstance(holder, Mapping) else {}
    )
    lane = journal.get("turn_lane")
    if isinstance(lane, Mapping) and lane:
        projection["turn_lane"] = dict(lane)
    return projection


def single_executor_per_turn_lane(
    execution_payload: TURN_LANE_EXECUTION_PAYLOAD,
) -> Callable[[TURN_LANE_TURN_RUNNER], TURN_LANE_TURN_RUNNER]:
    """Admit one executing Turn per lane and refuse the second with a typed packet.

    A non-executing decision (``execute=False``) takes no fence: it invokes no
    host and spends nothing, so it can always answer. The fence is held across
    the whole executing section, which is why it wraps the entry rather than one
    phase inside it. The refusal is rendered by the caller's own payload builder
    so a lane refusal and a host refusal keep exactly one payload shape.

    The caller passes ``turn_lane_todo_id`` (see :func:`turn_lane_todo_scope`)
    to run the Turn in its (goal, todo) lane instead of the agent lane.
    """

    def decorate(execute_turn: TURN_LANE_TURN_RUNNER) -> TURN_LANE_TURN_RUNNER:
        @wraps(execute_turn)
        def single_lane_turn(
            plan: Mapping[str, Any], *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            runtime_root = kwargs.get("runtime_root")
            goal_id = str(kwargs.get("goal_id") or "")
            if not kwargs.get("execute") or runtime_root is None or not goal_id:
                return execute_turn(plan, *args, **kwargs)
            root = Path(runtime_root)
            todo_id = str(kwargs.get("turn_lane_todo_id") or "").strip() or None
            target = turn_lane_target(
                runtime_root=root, goal_id=goal_id, plan=plan, todo_id=todo_id
            )
            with turn_lane_singleflight(
                runtime_root=root, goal_id=goal_id, plan=plan, todo_id=todo_id
            ) as held:
                if held is not None:
                    return execute_turn(plan, *args, **kwargs)
                return execution_payload(
                    plan,
                    turn_lane_in_flight_record(
                        plan,
                        holder=turn_lane_holder_readback(target),
                        todo_id=todo_id,
                    ),
                    execute=True,
                    replayed=False,
                    effects=TURN_LANE_NO_EFFECTS,
                )

        return single_lane_turn

    return decorate
