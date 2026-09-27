"""An orchestrator action todo whose Turns keep failing is retired (E2E pilot v1).

On a finished role_v1 goal a stale Next Action raised
``state_projection_gap_repair``; the dispatcher opened an action todo, and its
``todos-changed-since`` validator could never pass because nothing was left
to do. The todo stayed open and relaunched a real orchestrator Turn on every
backoff expiry. After two failed Turns the dispatcher now closes the action
todo and opens the repeat-limit user gate instead of spending more Turns.
"""

from __future__ import annotations

from pathlib import Path

from loopx.dispatch.orchestrator_actions import (
    ORCHESTRATOR_ACTION_FAILED_TURN_LIMIT,
    ORCHESTRATOR_ACTION_RETIRED_REASON,
    action_subject,
)
from loopx.todos import list_goal_todos
from tests.dispatch.dispatch_fixtures import GOAL_ID, read_jsonl, set_modes, write_fixture
from tests.dispatch.test_loopx_dispatcher import Clock, _action_should_run, _dispatcher, _orch_rows, _user_gates

ACTION = "state_projection_gap_repair"


def _gated_should_run(fixture: dict):
    """should-run for the pending action; like LoopX, an open user gate blocking orch holds its lane."""

    inner = _action_should_run(fixture, ACTION)

    def should_run(goal_id: str, agent_id: str) -> dict:
        if agent_id == "orch" and _user_gates(fixture):
            return {"should_run": False, "reason": "operator gate blocks gated delivery"}
        return inner(goal_id, agent_id)

    return should_run


def _pass(dispatcher, clock: Clock) -> dict:
    report = dispatcher.run_once()
    dispatcher.wait_for_children(list(dispatcher.children), timeout=30)
    clock.now += 7200  # past any todo backoff
    return report


def _orch_launches(fixture: dict) -> int:
    return sum(1 for item in read_jsonl(fixture["turn_log"]) if item["agent"] == "orch")


def test_action_subject_parses_both_kinds() -> None:
    assert action_subject({"text": f"Orchestrator action: settle the pending effective_action={ACTION}. Replan: x"}) \
        == f"effective_action={ACTION}"
    assert action_subject({"text": "Orchestrator action: answer the gate replies awaiting you: todo_a, todo_b. Read"}) \
        == "todo_a,todo_b"
    assert action_subject({"text": "Build the api"}) is None


def test_an_action_todo_whose_turns_keep_failing_is_retired_with_one_gate(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"orch": {"role": "orchestrator"}, "dev": {"role": "developer"}})
    set_modes(fixture, {"orch": ["failed"]})
    clock = Clock()
    dispatcher = _dispatcher(fixture, should_run=_gated_should_run(fixture), clock=clock)

    [opened] = _pass(dispatcher, clock)["orchestrator_todos_opened"]
    action_todo = opened["todo_id"]
    for _ in range(ORCHESTRATOR_ACTION_FAILED_TURN_LIMIT):
        launched = _pass(dispatcher, clock)["launched"]
        assert [(item["agent_id"], item["todo_id"]) for item in launched] == [("orch", action_todo)]
    assert _orch_launches(fixture) == ORCHESTRATOR_ACTION_FAILED_TURN_LIMIT

    retired = _pass(dispatcher, clock)
    assert retired["launched"] == [], retired
    assert {"goal_id": GOAL_ID, "agent_id": "orch", "reason": ORCHESTRATOR_ACTION_RETIRED_REASON,
            "todo_id": action_todo} in retired["skipped"], retired
    assert retired["orchestrator_todos_retired"][0]["failed_turns"] == ORCHESTRATOR_ACTION_FAILED_TURN_LIMIT
    row = next(item for item in _orch_rows(fixture) if item["todo_id"] == action_todo)
    assert row["status"] == "done"
    [gate] = _user_gates(fixture)
    assert gate["text"].startswith(f"Orchestrator orch did not settle effective_action={ACTION}: its Turns on "
                                   f"action todo {action_todo} failed 2 times")

    # The gate holds the orchestrator: no further Turn, no second gate or action todo.
    for _ in range(3):
        later = _pass(dispatcher, clock)
        assert later["launched"] == [] and "orchestrator_todos_opened" not in later, later
    assert _orch_launches(fixture) == ORCHESTRATOR_ACTION_FAILED_TURN_LIMIT
    assert len(_user_gates(fixture)) == 1


def test_host_failures_do_not_retire_an_action_todo(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"orch": {"role": "orchestrator"}, "dev": {"role": "developer"}})
    set_modes(fixture, {"orch": ["crash", "crash", "ok"]})
    clock = Clock()
    dispatcher = _dispatcher(fixture, should_run=_action_should_run(fixture, ACTION), clock=clock)
    [opened] = _pass(dispatcher, clock)["orchestrator_todos_opened"]
    for _ in range(3):
        report = _pass(dispatcher, clock)
        assert all(item.get("reason") != ORCHESTRATOR_ACTION_RETIRED_REASON for item in report["skipped"]), report
    assert not (dispatcher.state.get("orchestrator_action_failures") or {})
    listed = list_goal_todos(registry_path=fixture["registry"], goal_id=GOAL_ID, runtime_root_arg=str(fixture["runtime"]))
    assert next(row for row in listed["todos"] if row["todo_id"] == opened["todo_id"])["status"] == "open"
