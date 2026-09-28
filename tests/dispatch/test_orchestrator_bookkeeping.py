"""Decision 43: mechanical orchestrator bookkeeping is done by LoopX, not by a model Turn.

E2E pilot v1: after the user approved the plan card, a whole orchestrator Turn
($0.85) ran only to return validated_completion for the intake planning todo.
LoopX now closes it when the plan is applied, and the dispatcher closes other
pure-bookkeeping orchestrator todos (a planning todo whose plan is already
applied, an escalation whose todo is already done, a gate-reply action todo
whose gates were already answered or settled) before it would launch a Turn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from loopx.dispatch.dispatcher import ORCHESTRATOR_BOOKKEEPING_LIMIT_REASON, ORCHESTRATOR_BOOKKEEPING_PASS_LIMIT
from loopx.gate_threads import reply_to_gate
from loopx.orchestrator_bookkeeping import (
    BOOKKEEPING_KIND_ESCALATION,
    BOOKKEEPING_KIND_GATE_ACTION,
    BOOKKEEPING_KIND_PLANNING,
    mechanical_orchestrator_closeout,
)
from loopx.plan_cards import propose_plan
from loopx.todos import add_goal_todo, complete_goal_todo, list_goal_todos
from tests.dispatch.dispatch_fixtures import GOAL_ID, read_jsonl, write_fixture
from tests.dispatch.test_loopx_dispatcher import _action_should_run, _dispatcher

PLAN = {
    "title": "Two steps",
    "summary": "Write the doc, then the code.",
    "todos": [
        {"key": "doc", "text": "Write the doc", "bound_agent": "dev", "requires_acceptance": False},
        {"key": "code", "text": "Write the code", "bound_agent": "dev", "depends_on": ["doc"],
         "requires_acceptance": False},
    ],
}


def _fixture(tmp_path: Path, *, model: str = "role_v1") -> dict[str, Any]:
    fixture = write_fixture(tmp_path, agents={"orch": {"role": "orchestrator"}, "dev": {"role": "developer"}},
                            agent_model=model)
    return fixture


def _kw(fixture: dict[str, Any]) -> dict[str, Any]:
    return {"registry_path": fixture["registry"], "goal_id": GOAL_ID, "runtime_root_arg": str(fixture["runtime"])}


def _add(fixture: dict[str, Any], text: str, **extra: Any) -> str:
    return str(add_goal_todo(**_kw(fixture), role="agent", text=text, task_class="advancement_task", **extra)["todo_id"])


def _add_planning(fixture: dict[str, Any]) -> str:
    return _add(fixture, "Clarify the requirements, then propose the initial plan card", action_kind="plan",
                claimed_by="orch", role_contract={"required_role": "orchestrator", "requires_acceptance": False})


def _rows(fixture: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["todo_id"]: row for row in list_goal_todos(**_kw(fixture))["todos"]}


def _approve(fixture: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    record = propose_plan(registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID,
                          agent_id="orch", plan=plan, runtime_root_arg=str(fixture["runtime"]))["plan"]
    decided = complete_goal_todo(**_kw(fixture), todo_id=record["gate_todo_id"], role="user",
                                 decision_outcome="approve", agent_id="orch")
    assert decided.get("ok") is not False, decided
    return decided["plan_card"]


def _orch_launches(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in read_jsonl(fixture["turn_log"]) if item["agent"] == "orch"]


def test_plan_apply_closes_the_planning_todo_and_launches_no_orchestrator_turn(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    planning = _add_planning(fixture)
    card = _approve(fixture, PLAN)

    [closed] = card["planning_todos_closed"]
    assert closed["closed"] is True and closed["todo_id"] == planning and closed["kind"] == BOOKKEEPING_KIND_PLANNING
    row = _rows(fixture)[planning]
    assert row["status"] == "done"
    # The evidence names the plan and every todo it created.
    assert f"plan_applied={card['plan_id']}" in row["evidence"]
    for key, todo_id in card["todo_id_map"].items():
        assert f"{key}={todo_id}" in row["evidence"]
    assert "decision 43" in row["note"]

    # With the real should-run the orchestrator has nothing left: no Turn is launched.
    report = _dispatcher(fixture).run_once()
    assert _orch_launches(fixture) == [], report
    assert "orchestrator_todos_opened" not in report
    [orch] = [item for item in report["skipped"] if item["agent_id"] == "orch"]
    assert orch["reason"] in {"orchestrator_idle", "should_run_false"}, orch
    # The plan's first todo goes straight to the developer.
    assert [(item["agent_id"], item["todo_id"]) for item in report["launched"]] == [("dev", card["todo_id_map"]["doc"])]


def test_a_criteria_only_card_and_a_planned_planning_todo_are_left_to_the_orchestrator(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    # A plan that itself creates an orchestrator planning step: this card did not answer that step.
    plan = {"title": "Phase 1", "todos": [
        {"key": "doc", "text": "Write the doc", "bound_agent": "dev", "requires_acceptance": False},
        {"key": "phase2", "text": "Plan phase 2", "required_role": "orchestrator", "bound_agent": "orch",
         "action_kind": "plan", "requires_acceptance": False},
    ]}
    card = _approve(fixture, plan)
    assert "planning_todos_closed" not in card
    phase2 = card["todo_id_map"]["phase2"]
    assert _rows(fixture)[phase2]["status"] == "open"
    assert mechanical_orchestrator_closeout(
        registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID,
        todo=_rows(fixture)[phase2], runtime_root_arg=str(fixture["runtime"]),
    ) is None
    dispatcher = _dispatcher(fixture, should_run=_action_should_run(fixture, None))
    report = dispatcher.run_once()
    assert [(item["agent_id"], item["todo_id"]) for item in report["launched"]] == [("orch", phase2)]


def test_the_dispatcher_closes_a_planning_todo_whose_plan_is_already_applied(tmp_path: Path) -> None:
    """Backstop: a planning todo left open after its plan was applied (older goals, a failed closeout)."""

    fixture = _fixture(tmp_path)
    _approve(fixture, PLAN)
    planning = _add_planning(fixture)  # open, and no applied plan created it
    dispatcher = _dispatcher(fixture, should_run=_action_should_run(fixture, None))
    report = dispatcher.run_once()
    assert report["launched"] == [] and _orch_launches(fixture) == [], report
    [closed] = report["orchestrator_todos_closed"]
    assert closed["todo_id"] == planning and closed["kind"] == BOOKKEEPING_KIND_PLANNING
    assert _rows(fixture)[planning]["status"] == "done"


def _escalation(fixture: dict[str, Any], target: str) -> str:
    return _add(fixture, f"Escalation: {target} was rejected 2 times by acc. Decide: reassign, split.",
                action_kind="replan", claimed_by="orch",
                role_contract={"required_role": "orchestrator", "requires_acceptance": False})


def test_an_escalation_whose_todo_is_already_done_is_closed_without_a_turn(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    target = _add(fixture, "Build the web UI", action_kind="fixture", claimed_by="dev")
    escalation = _escalation(fixture, target)
    done = complete_goal_todo(**_kw(fixture), todo_id=target, role="agent", evidence="owner closed it", agent_id="dev")
    assert done.get("ok") is not False, done
    report = _dispatcher(fixture, should_run=_action_should_run(fixture, None)).run_once()
    assert _orch_launches(fixture) == [], report
    [closed] = report["orchestrator_todos_closed"]
    assert closed["kind"] == BOOKKEEPING_KIND_ESCALATION and closed["todo_id"] == escalation
    row = _rows(fixture)[escalation]
    assert row["status"] == "done" and f"escalated_todo={target} status=done" in row["evidence"]


def test_an_escalation_on_a_still_blocked_todo_launches_the_orchestrator(tmp_path: Path) -> None:
    from loopx.todos import update_goal_todo

    fixture = _fixture(tmp_path)
    target = _add(fixture, "Build the web UI", action_kind="fixture", claimed_by="dev")
    update_goal_todo(**_kw(fixture), todo_id=target, role="agent", status="blocked", reason="rejected twice",
                     agent_id="dev")
    escalation = _escalation(fixture, target)
    report = _dispatcher(fixture, should_run=_action_should_run(fixture, None)).run_once()
    assert "orchestrator_todos_closed" not in report
    assert [(item["agent_id"], item["todo_id"]) for item in report["launched"]] == [("orch", escalation)]


def _gate_with_action_todo(fixture: dict[str, Any]) -> tuple[str, str]:
    gate = str(add_goal_todo(**_kw(fixture), role="user", task_class="user_gate", agent_id="orch",
                             text="Which database?")["todo_id"])
    reply_to_gate(registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID,
                  todo_id=gate, text="Use sqlite")
    dispatcher = _dispatcher(fixture, should_run=_action_should_run(fixture, None))
    [opened] = dispatcher.run_once()["orchestrator_todos_opened"]
    return gate, opened["todo_id"]


def test_a_gate_action_todo_whose_gate_was_already_answered_is_closed_without_a_turn(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    gate, action = _gate_with_action_todo(fixture)
    # Another Turn answered the reply before the action todo ran.
    reply_to_gate(registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID,
                  todo_id=gate, text="Noted: sqlite.", author="orchestrator", agent_id="orch")
    report = _dispatcher(fixture, should_run=_action_should_run(fixture, None)).run_once()
    assert _orch_launches(fixture) == [], report
    [closed] = report["orchestrator_todos_closed"]
    assert closed["kind"] == BOOKKEEPING_KIND_GATE_ACTION and closed["todo_id"] == action
    assert _rows(fixture)[action]["status"] == "done"
    assert _rows(fixture)[gate]["status"] == "open"  # the gate itself is the user's to close


def test_an_unanswered_gate_reply_still_launches_the_orchestrator(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    gate, action = _gate_with_action_todo(fixture)
    # The user closed the question gate after replying: what the reply asked is judgment.
    complete_goal_todo(**_kw(fixture), todo_id=gate, role="user", decision_outcome="approve", agent_id="orch")
    report = _dispatcher(fixture, should_run=_action_should_run(fixture, None)).run_once()
    assert "orchestrator_todos_closed" not in report
    assert [(item["agent_id"], item["todo_id"]) for item in report["launched"]] == [("orch", action)]


def test_the_dispatcher_closes_at_most_the_pass_limit_before_it_launches(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    targets = [_add(fixture, f"Work {index}", action_kind="fixture", claimed_by="dev")
               for index in range(ORCHESTRATOR_BOOKKEEPING_PASS_LIMIT + 1)]
    escalations = [_escalation(fixture, target) for target in targets]
    for target in targets:
        complete_goal_todo(**_kw(fixture), todo_id=target, role="agent", evidence="done", agent_id="dev")
    dispatcher = _dispatcher(fixture, should_run=_action_should_run(fixture, None))
    first = dispatcher.run_once()
    assert len(first["orchestrator_todos_closed"]) == ORCHESTRATOR_BOOKKEEPING_PASS_LIMIT
    assert first["launched"] == []
    assert {"goal_id": GOAL_ID, "agent_id": "orch", "reason": ORCHESTRATOR_BOOKKEEPING_LIMIT_REASON,
            "todo_id": escalations[-1]} in first["skipped"], first
    # The limit bounds one pass; the next pass closes the rest, still with no Turn.
    second = dispatcher.run_once()
    assert [item["todo_id"] for item in second["orchestrator_todos_closed"]] == escalations[-1:]
    assert _orch_launches(fixture) == []


def test_peer_v1_goals_are_unchanged(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, model="peer_v1")
    registry = json.loads(fixture["registry"].read_text(encoding="utf-8"))
    assert registry["goals"][0]["coordination"]["agent_model"] == "peer_v1"
    planning = _add(fixture, "Plan the goal", action_kind="plan", claimed_by="orch")
    target = _add(fixture, "Build it", action_kind="fixture", claimed_by="dev")
    escalation = _escalation(fixture, target)
    complete_goal_todo(**_kw(fixture), todo_id=target, role="agent", evidence="done", agent_id="dev")
    for todo_id in (planning, escalation):
        assert mechanical_orchestrator_closeout(
            registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID,
            todo=_rows(fixture)[todo_id], runtime_root_arg=str(fixture["runtime"]),
        ) is None
    from loopx.orchestrator_bookkeeping import close_planning_todos_after_apply

    assert close_planning_todos_after_apply(
        registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID,
        record={"plan_id": "plan_x", "todo_id_map": {"a": target}}, runtime_root_arg=str(fixture["runtime"]),
    ) == []
    assert _rows(fixture)[planning]["status"] == "open"


@pytest.mark.parametrize("status", ["done", "blocked"])
def test_only_open_todos_are_closed(tmp_path: Path, status: str) -> None:
    fixture = _fixture(tmp_path)
    assert mechanical_orchestrator_closeout(
        registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID,
        todo={"todo_id": "todo_x", "role": "agent", "status": status, "action_kind": "plan",
              "required_role": "orchestrator"},
    ) is None
