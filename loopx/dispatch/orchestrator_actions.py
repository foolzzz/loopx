"""Orchestrator action todos: give a todo-less orchestrator action a real Turn.

``turn run-once`` refuses host routes without todo lineage, so the
dispatcher never launches a todo-less orchestrator Turn (policy.decide_turn).
Two states then stalled a goal silently (review of the E2E pilot fixes):

* should-run reports a pending orchestrator action but selects no todo, for
  example an S1-routed required replan obligation
  (``orchestrator_action_without_todo``);
* a user replied on a gate thread, so the gate awaits the orchestrator, but
  the orchestrator holds no open todo that would wake its lane (gap G7).

For either the dispatcher opens one orchestrator todo through the ordinary
Todo API, like the S2 escalation todo, so the next pass launches a Turn for
it. It is idempotent and one at a time: an open action todo, or any open
orchestrator todo, suppresses another, and the text prefix finds it again
when the dispatcher state is lost. The Turn validator is chosen from the
todo text: the listed gates no longer await the orchestrator, or the
orchestrator changed some other todo during the Turn.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

ORCHESTRATOR_ACTION_TEXT_PREFIX = "Orchestrator action: "
_AWAITING_GATES_MARKER = "answer the gate replies awaiting you: "
_ACTION_MARKER = "settle the pending effective_action="
_TODO_TOKEN = re.compile(r"todo_[A-Za-z0-9_-]+")
# An action todo that is open, under review or blocked still owns the action.
_ACTION_TODO_LIVE_STATUSES = frozenset({"open", "in_review", "blocked"})
# Any other open orchestrator todo already wakes the orchestrator's lane.
_ORCHESTRATOR_TODO_LIVE_STATUSES = frozenset({"open", "in_review"})
# The same effective action reopened this many times is escalated to the user.
ORCHESTRATOR_ACTION_REPEAT_LIMIT = 2


def is_orchestrator_action_todo(todo: Mapping[str, Any] | None) -> bool:
    return isinstance(todo, Mapping) and str(todo.get("text") or "").startswith(ORCHESTRATOR_ACTION_TEXT_PREFIX)


def action_gate_ids(todo: Mapping[str, Any] | None) -> list[str]:
    """The gates an action todo was opened for, parsed from its own text."""

    if not is_orchestrator_action_todo(todo):
        return []
    text = str((todo or {}).get("text") or "")
    if _AWAITING_GATES_MARKER not in text:
        return []
    listed = text.split(_AWAITING_GATES_MARKER, 1)[1].split(".", 1)[0]
    return _TODO_TOKEN.findall(listed)


def action_todo_text(effective_action: str | None, gate_ids: Sequence[str]) -> str:
    if gate_ids:
        return (
            f"{ORCHESTRATOR_ACTION_TEXT_PREFIX}{_AWAITING_GATES_MARKER}{', '.join(gate_ids)}. "
            "Read each thread with `loopx gate show`, then reply, open follow-up todos or a plan, "
            "or close the gate."
        )
    return (
        f"{ORCHESTRATOR_ACTION_TEXT_PREFIX}{_ACTION_MARKER}{effective_action}. Replan: open typed "
        "follow-up todos, propose a plan, open a user gate, or record the goal's terminal outcome."
    )


def live_orchestrator_todo(todos: Iterable[Mapping[str, Any]], agent_id: str) -> str | None:
    """An existing todo that already carries (or wakes) the orchestrator's work."""

    for row in todos:
        if not isinstance(row, Mapping) or str(row.get("role") or "agent") != "agent":
            continue
        status = str(row.get("status") or "")
        if is_orchestrator_action_todo(row) and status in _ACTION_TODO_LIVE_STATUSES:
            return str(row.get("todo_id") or "") or None
        if row.get("claimed_by") == agent_id and status in _ORCHESTRATOR_TODO_LIVE_STATUSES:
            return str(row.get("todo_id") or "") or None
    return None


def action_validator_argv(
    python: str, *, registry: str, runtime_root: str, goal_id: str, todo: Mapping[str, Any],
) -> list[str]:
    """The independent Turn validator of an action todo."""

    common = ["--registry", registry, "--runtime-root", runtime_root, "--goal-id", goal_id]
    gates = action_gate_ids(todo)
    if gates:
        argv = [python, "-m", "loopx.dispatch.checks", "gates-not-awaiting", *common]
        for gate_id in gates:
            argv.extend(["--gate-id", gate_id])
        return argv
    # Composed at launch: some other todo must be created or changed during
    # this Turn (a follow-up todo, a plan gate, a user gate, a closed todo).
    since = datetime.now().astimezone().isoformat(timespec="seconds")
    return [
        python, "-m", "loopx.dispatch.checks", "todos-changed-since", *common,
        "--exclude-todo-id", str(todo.get("todo_id") or ""), "--since", since,
    ]
