"""Budget exhausted pauses a goal's new Turns (design decision 41).

With a budget, the dispatcher keeps the non-blocking 80% alert; at 100% it
opens one ``budget_exhausted`` user gate per crossing and launches no new
Turn of that goal (running Turns finish). The owner raises the budget,
continues without a limit or stops the goal, through ``loopx gate resolve``
or the dashboard ``gate.resolve``.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from loopx.agent_registry import load_goal_from_registry
from loopx.chat_action_store import ChatActionStore
from loopx.chat_actions import ChatActionService
from loopx.cli import main as cli_main
from loopx.control_plane.goals.activation import goal_is_stopped
from loopx.control_plane.goals.activation_service import set_goal_activation_state
from loopx.dispatch import DispatchConfig, Dispatcher
from loopx.gate_threads import gate_view, read_gate_index
from loopx.rollout_event_log import rollout_event_log_path
from loopx.todos import list_goal_todos
from loopx.usage_accounting import read_usage_budget, record_turn_usage, write_usage_budget
from loopx.usage_budget_gate import (
    BUDGET_GATE_OPTIONS,
    BUDGET_GATE_TEXT_PREFIX,
    BUDGET_HOLD_GATE_OPEN,
    BUDGET_HOLD_STOPPED,
    default_raised_budget,
    parse_budget_amount,
    settle_budget_gate,
)
from tests.dispatch.dispatch_fixtures import GOAL_ID, set_modes, write_fixture
from tests.dispatch.test_loopx_dispatcher import Clock
from tests.usage_accounting.test_usage_ledger_report import _claude_usage

OTHER_GOAL = "budget-other"
TURN_COST = 0.047  # one fixture claude Turn, host reported (approximately)


class GoalShouldRun:
    """should-run per (goal, agent): a fixed queue of todos, else no work."""

    def __init__(self, todos: dict[tuple[str, str], list[str]]) -> None:
        self.todos = {key: list(queue) for key, queue in todos.items()}

    def __call__(self, goal_id: str, agent_id: str) -> dict[str, Any]:
        queue = self.todos.get((goal_id, agent_id)) or []
        if not queue:
            return {"should_run": False, "reason": "no eligible work"}
        todo_id = queue.pop(0) if len(queue) > 1 else queue[0]
        return {"should_run": True, "effective_action": "normal_run",
                "selected_todo": {"todo_id": todo_id, "role": "agent"}}


def _fixture(tmp_path: Path, *, other_goal: bool = False) -> dict[str, Any]:
    fixture = write_fixture(tmp_path, agents={
        "orch": {"role": "orchestrator"},
        "dev": {"role": "developer", "max_concurrency": 3},
        "acc": {"role": "acceptor"},
    })
    fixture["goal_ids"] = [GOAL_ID]
    if other_goal:
        registry = json.loads(fixture["registry"].read_text(encoding="utf-8"))
        goal = json.loads(json.dumps(registry["goals"][0]))
        state = fixture["project"] / ".loopx" / "goals" / OTHER_GOAL / "ACTIVE_GOAL_STATE.md"
        state.parent.mkdir(parents=True)
        state.write_text(fixture["state"].read_text(encoding="utf-8"), encoding="utf-8")
        goal.update(id=OTHER_GOAL, state_file=str(state.relative_to(fixture["project"])))
        registry["goals"].append(goal)
        fixture["registry"].write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
        fixture["goal_ids"].append(OTHER_GOAL)
    return fixture


def _dispatcher(fixture: dict[str, Any], should_run: GoalShouldRun, clock: Clock | None = None) -> Dispatcher:
    config = DispatchConfig(
        registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_ids=list(fixture["goal_ids"]),
        no_global_sync=True, environ=fixture["environ"], loopx_argv=(sys.executable, str(fixture["fake_loopx"])),
    )
    return Dispatcher(config, should_run=should_run, clock=clock or Clock())


def _spend(fixture: dict[str, Any], turns: int, *, goal_id: str = GOAL_ID, start: int = 0) -> None:
    for index in range(start, start + turns):
        record_turn_usage(fixture["runtime"], goal_id, _claude_usage(), agent_id="dev", role="developer",
                          todo_id=f"todo_s{index}", turn_key="sha256:" + f"{index:x}".rjust(64, "0"),
                          status="committed")


def _open_user(fixture: dict[str, Any], goal_id: str = GOAL_ID) -> list[dict[str, Any]]:
    return list_goal_todos(registry_path=fixture["registry"], goal_id=goal_id, role="user", status="open",
                           runtime_root_arg=str(fixture["runtime"]))["todos"]


def _budget_gates(fixture: dict[str, Any], *, open_only: bool = True) -> list[dict[str, Any]]:
    index = read_gate_index(fixture["runtime"], GOAL_ID)["gates"]
    rows = list_goal_todos(registry_path=fixture["registry"], goal_id=GOAL_ID, role="user",
                           runtime_root_arg=str(fixture["runtime"]))["todos"]
    return [row for row in rows if (index.get(row["todo_id"]) or {}).get("kind") == "budget_exhausted"
            and (row["status"] == "open" or not open_only)]


def _cli(fixture: dict[str, Any], *argv: str) -> tuple[int, dict[str, Any]]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_main(["--registry", str(fixture["registry"]), "--runtime-root", str(fixture["runtime"]),
                         "--format", "json", *argv])
    return code, json.loads(buffer.getvalue())


def _events(fixture: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    path = rollout_event_log_path(fixture["runtime"], GOAL_ID)
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("event_kind") == kind]


def _launched(report: dict[str, Any], goal_id: str = GOAL_ID) -> list[tuple[str, str]]:
    return [(item["agent_id"], item.get("todo_id")) for item in report["launched"] if item["goal_id"] == goal_id]


def _held(report: dict[str, Any], goal_id: str = GOAL_ID) -> set[tuple[str, str]]:
    return {(item["agent_id"], item["reason"]) for item in report["skipped"] if item["goal_id"] == goal_id}


def _exhaust(fixture: dict[str, Any], dispatcher: Dispatcher) -> str:
    """Spend above a $0.05 budget and let one pass open the gate."""

    write_usage_budget(fixture["runtime"], GOAL_ID, 0.05)
    _spend(fixture, 2)
    report = dispatcher.reconcile()
    opened = [item for item in report["gates_opened"] if item["key"] == "usage_budget:100"]
    assert len(opened) == 1, report
    return str(opened[0]["todo_id"])


# --- thresholds -------------------------------------------------------------------------


def test_the_80_percent_alert_stays_non_blocking(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    write_usage_budget(fixture["runtime"], GOAL_ID, 0.05)
    _spend(fixture, 1)  # ~94%
    dispatcher = _dispatcher(fixture, GoalShouldRun({(GOAL_ID, "dev"): ["todo_aaa"]}))
    report = dispatcher.reconcile()
    assert [item["key"] for item in report["gates_opened"]] == ["usage_budget:80"]
    [alert] = _open_user(fixture)
    assert alert["task_class"] == "user_action" and not alert.get("blocks_agent")
    assert _budget_gates(fixture) == []
    assert _launched(report) == [("dev", "todo_aaa")], "the 80% alert never pauses work"
    assert dispatcher.reconcile()["gates_opened"] == [], "the 80% alert opens once"
    dispatcher.wait_for_children(list(dispatcher.children), timeout=30)


def test_no_budget_means_no_gate_and_no_hold(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _spend(fixture, 5)
    dispatcher = _dispatcher(fixture, GoalShouldRun({(GOAL_ID, "dev"): ["todo_aaa"]}))
    report = dispatcher.reconcile()
    assert report["gates_opened"] == [] and report["errors"] == []
    assert _launched(report) == [("dev", "todo_aaa")]
    assert _open_user(fixture) == [] and read_gate_index(fixture["runtime"], GOAL_ID)["gates"] == {}
    dispatcher.wait_for_children(list(dispatcher.children), timeout=30)


def test_100_percent_opens_one_gate_and_holds_new_turns_while_running_turns_finish(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    set_modes(fixture, {"dev": ["hold"]})
    should_run = GoalShouldRun({(GOAL_ID, "dev"): ["todo_aaa", "todo_bbb"], (GOAL_ID, "orch"): ["todo_orc"],
                                (GOAL_ID, "acc"): ["todo_rev"]})
    dispatcher = _dispatcher(fixture, should_run)
    write_usage_budget(fixture["runtime"], GOAL_ID, 0.05)
    first = dispatcher.reconcile()
    assert ("dev", "todo_aaa") in _launched(first)
    running = {run_id: run["pid"] for run_id, run in dispatcher.state["runs"].items() if run["agent_id"] == "dev"}
    assert running

    # Spend jumps past 100% at once: the gate covers the 80% alert too.
    _spend(fixture, 2)
    second = dispatcher.reconcile()
    assert [item["key"] for item in second["gates_opened"]] == ["usage_budget:100"]
    assert _launched(second) == [], "no new Turn for any role"
    held = _held(second)
    assert {("orch", BUDGET_HOLD_GATE_OPEN), ("dev", BUDGET_HOLD_GATE_OPEN), ("acc", BUDGET_HOLD_GATE_OPEN)} <= held
    for run_id, pid in running.items():
        assert run_id in dispatcher.state["runs"] and dispatcher.state["runs"][run_id]["pid"] == pid, (
            "running Turns are never killed"
        )
    [gate] = _budget_gates(fixture)
    assert [row["task_class"] for row in _open_user(fixture)] == ["user_gate"], "no separate 100% user_action"
    assert gate["task_class"] == "user_gate" and gate.get("blocks_agent") == "orch"
    text = gate["text"]
    assert text.startswith(BUDGET_GATE_TEXT_PREFIX)
    assert "spent $0.09 of its $0.05 budget" in text and "estimated" in text and "By role: developer $0.09" in text
    assert "default +50% = $0.0" in text and "running Turns finish" in text
    view = gate_view(registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID,
                     todo_id=gate["todo_id"])
    assert view["kind"] == "budget_exhausted" and view["options"] == list(BUDGET_GATE_OPTIONS)
    assert view["budget_usd"] == 0.05 and view["by_role"][0]["role"] == "developer"
    [event] = _events(fixture, "usage_budget_exhausted")
    assert event["todo_id"] == gate["todo_id"]

    # The running Turn finishes normally.
    fixture["release"].write_text("go", encoding="utf-8")
    finished = dispatcher.wait_for_children(list(dispatcher.children), timeout=30)
    assert finished and {item["outcome"] for item in finished} == {"committed"}
    assert set(running) <= {item["run_id"] for item in finished}
    assert _launched(dispatcher.reconcile()) == []


def test_replayed_and_restarted_dispatchers_never_reopen_the_gate(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    clock = Clock()
    gate_id = _exhaust(fixture, _dispatcher(fixture, GoalShouldRun({}), clock))
    # A replayed tick, a fresh dispatcher and a lost state file.
    assert _dispatcher(fixture, GoalShouldRun({}), clock).reconcile()["gates_opened"] == []
    (fixture["runtime"] / "dispatch" / "state.json").unlink()
    again = _dispatcher(fixture, GoalShouldRun({}), clock)
    assert again.reconcile()["gates_opened"] == [] and again.reconcile()["gates_opened"] == []
    _spend(fixture, 1, start=10)
    assert again.reconcile()["gates_opened"] == [], "more spend is the same crossing"
    assert [gate["todo_id"] for gate in _budget_gates(fixture, open_only=False)] == [gate_id]
    assert len(_events(fixture, "usage_budget_exhausted")) == 1
    # A stop leaves the crossing closed: the resumed goal is not gated again
    # until the budget changes.
    code, _payload = _cli(fixture, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", gate_id,
                          "--option", "stop_goal")
    assert code == 0
    set_goal_activation_state(registry_path=fixture["registry"], goal_id=GOAL_ID, state="active",
                              runtime_root_override=str(fixture["runtime"]), actor_kind="owner", execute=True)
    assert again.reconcile()["gates_opened"] == []
    assert len(_budget_gates(fixture, open_only=False)) == 1


def test_other_goals_keep_running(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, other_goal=True)
    should_run = GoalShouldRun({(GOAL_ID, "dev"): ["todo_aaa"], (OTHER_GOAL, "dev"): ["todo_ooo"]})
    dispatcher = _dispatcher(fixture, should_run)
    write_usage_budget(fixture["runtime"], GOAL_ID, 0.05)
    _spend(fixture, 2)
    report = dispatcher.reconcile()
    assert [item["key"] for item in report["gates_opened"]] == ["usage_budget:100"]
    assert _launched(report, GOAL_ID) == []
    assert _launched(report, OTHER_GOAL) == [("dev", "todo_ooo")]
    assert not any(item["reason"].startswith("budget") for item in report["skipped"]
                   if item["goal_id"] == OTHER_GOAL)
    dispatcher.wait_for_children(list(dispatcher.children), timeout=30)


# --- options ---------------------------------------------------------------------------


def test_raise_budget_with_a_note_amount_resumes_and_rearms_the_alerts(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    dispatcher = _dispatcher(fixture, GoalShouldRun({(GOAL_ID, "dev"): ["todo_aaa"]}))
    set_modes(fixture, {"dev": ["hold"]})
    gate_id = _exhaust(fixture, dispatcher)
    # A raise that does not cover the spend is refused; the gate stays open.
    code, payload = _cli(fixture, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", gate_id,
                         "--option", "raise_budget", "--note", "$0.08")
    assert code == 1 and "does not cover the spend" in payload["error"]
    assert len(_budget_gates(fixture)) == 1

    code, payload = _cli(fixture, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", gate_id,
                         "--option", "raise_budget", "--note", "raise to $0.11 please")
    assert code == 0, payload
    settled = payload["budget_gate"]
    assert settled["ok"] is True and settled["option"] == "raise_budget" and settled["budget_usd"] == 0.11
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == 0.11
    report = dispatcher.reconcile()
    assert _launched(report) == [("dev", "todo_aaa")], "dispatching resumes"
    # ~85% of the new budget: the 80% alert re-armed for the new value.
    assert [item["key"] for item in report["gates_opened"]] == ["usage_budget:80"]
    [decided] = _events(fixture, "usage_budget_decided")
    assert decided["status"] == "raise_budget"

    # Crossing the new budget opens a new gate.
    _spend(fixture, 1, start=20)
    later = dispatcher.reconcile()
    assert [item["key"] for item in later["gates_opened"]] == ["usage_budget:100"]
    assert len(_budget_gates(fixture)) == 1 and len(_budget_gates(fixture, open_only=False)) == 2
    fixture["release"].write_text("go", encoding="utf-8")
    dispatcher.wait_for_children(list(dispatcher.children), timeout=30)


def test_raise_budget_defaults_to_fifty_percent_more(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    write_usage_budget(fixture["runtime"], GOAL_ID, 0.08)
    _spend(fixture, 2)  # ~$0.094 of $0.08
    dispatcher = _dispatcher(fixture, GoalShouldRun({}))
    [opened] = dispatcher.reconcile()["gates_opened"]
    code, payload = _cli(fixture, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", opened["todo_id"],
                         "--decision", "approve")
    assert code == 0, payload
    assert payload["budget_gate"]["option"] == "raise_budget" and payload["budget_gate"]["budget_usd"] == 0.12
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == 0.12
    assert not any(item["reason"].startswith("budget") for item in dispatcher.reconcile()["skipped"])


def test_continue_without_limit_clears_the_budget(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    dispatcher = _dispatcher(fixture, GoalShouldRun({(GOAL_ID, "dev"): ["todo_aaa"]}))
    set_modes(fixture, {"dev": ["hold"]})
    gate_id = _exhaust(fixture, dispatcher)
    code, payload = _cli(fixture, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", gate_id,
                         "--option", "continue_without_limit")
    assert code == 0, payload
    assert payload["budget_gate"]["budget_usd"] is None and payload["budget_gate"]["previous_budget_usd"] == 0.05
    assert read_usage_budget(fixture["runtime"], GOAL_ID) is None
    _spend(fixture, 3, start=30)
    report = dispatcher.reconcile()
    assert _launched(report) == [("dev", "todo_aaa")] and report["gates_opened"] == []
    fixture["release"].write_text("go", encoding="utf-8")
    dispatcher.wait_for_children(list(dispatcher.children), timeout=30)


def test_stop_goal_records_the_decision_stops_the_goal_and_resumes_through_goal_lifecycle(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    dispatcher = _dispatcher(fixture, GoalShouldRun({(GOAL_ID, "dev"): ["todo_aaa"]}))
    set_modes(fixture, {"dev": ["hold"]})
    gate_id = _exhaust(fixture, dispatcher)
    agent_todos_before = list_goal_todos(registry_path=fixture["registry"], goal_id=GOAL_ID, role="agent",
                                         runtime_root_arg=str(fixture["runtime"]))["todos"]
    code, payload = _cli(fixture, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", gate_id,
                         "--decision", "cancel", "--option", "stop_goal")
    assert code == 0, payload
    settled = payload["budget_gate"]
    assert settled["ok"] is True and settled["option"] == "stop_goal"
    assert "goal-lifecycle" in settled["resume"] and "--operation resume" in settled["resume"]
    assert goal_is_stopped(load_goal_from_registry(fixture["registry"], GOAL_ID))
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == 0.05, "stop keeps the budget"
    entry = read_gate_index(fixture["runtime"], GOAL_ID)["gates"][gate_id]
    assert entry["decision_option"] == "stop_goal" and entry["budget_outcome"]["option"] == "stop_goal"
    assert list_goal_todos(registry_path=fixture["registry"], goal_id=GOAL_ID, role="agent",
                           runtime_root_arg=str(fixture["runtime"]))["todos"] == agent_todos_before
    report = dispatcher.reconcile()
    assert _launched(report) == [] and ("dev", BUDGET_HOLD_STOPPED) in _held(report)

    resumed = set_goal_activation_state(registry_path=fixture["registry"], goal_id=GOAL_ID, state="active",
                                        runtime_root_override=str(fixture["runtime"]), actor_kind="owner",
                                        execute=True)
    assert resumed["ok"] is True
    assert _launched(dispatcher.reconcile()) == [("dev", "todo_aaa")]
    fixture["release"].write_text("go", encoding="utf-8")
    dispatcher.wait_for_children(list(dispatcher.children), timeout=30)


def test_mismatched_option_decisions_are_refused(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    gate_id = _exhaust(fixture, _dispatcher(fixture, GoalShouldRun({})))
    code, payload = _cli(fixture, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", gate_id,
                         "--decision", "reject", "--option", "raise_budget")
    assert code == 1 and "records decision approve" in payload["error"]
    code, payload = _cli(fixture, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", gate_id,
                         "--option", "retry_acceptance")
    assert code == 1 and "budget gate option must be one of" in payload["error"]
    assert len(_budget_gates(fixture)) == 1


def test_parse_budget_amount() -> None:
    assert parse_budget_amount("$75") == 75.0
    assert parse_budget_amount("raise to 1,200.50 USD") == 1200.5
    assert parse_budget_amount("") is None and parse_budget_amount("go on") is None


# --- web -------------------------------------------------------------------------------


@pytest.mark.parametrize("option", BUDGET_GATE_OPTIONS)
def test_each_option_applies_via_the_web_resolve_path(tmp_path: Path, option: str) -> None:
    fixture = _fixture(tmp_path)
    dispatcher = _dispatcher(fixture, GoalShouldRun({}))
    gate_id = _exhaust(fixture, dispatcher)
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=fixture["registry"])
    proposal = service.preview({
        "action_kind": "gate.resolve", "summary": f"{option} the budget",
        "normalized_parameters": {"goal_id": GOAL_ID, "todo_id": gate_id, "option": option, "note": "$0.2"},
        "context": {}, "idempotency_key": f"budget-{option}",
    })
    assert proposal["status"] == "preview_ready", proposal
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == 0.05, "the preview is a dry run"
    applied = service.apply(proposal["proposal_id"])["proposal"]
    assert applied["status"] == "applied", applied
    assert applied["receipt"]["decision_option"] == option
    entry = read_gate_index(fixture["runtime"], GOAL_ID)["gates"][gate_id]
    assert entry["budget_outcome"]["option"] == option and entry["budget_outcome"]["ok"] is True
    stopped = goal_is_stopped(load_goal_from_registry(fixture["registry"], GOAL_ID))
    budget = read_usage_budget(fixture["runtime"], GOAL_ID)
    expected = {"raise_budget": (0.2, False), "continue_without_limit": (None, False),
                "stop_goal": (0.05, True)}[option]
    assert (budget, stopped) == expected
    held = _held(dispatcher.reconcile())
    assert any(reason.startswith("budget") for _agent, reason in held) is (option == "stop_goal")


def _raise_budget_preview(
    tmp_path: Path, *, option: str | None = "raise_budget",
) -> tuple[dict[str, Any], str, ChatActionService, dict[str, Any]]:
    fixture = _fixture(tmp_path)
    dispatcher = _dispatcher(fixture, GoalShouldRun({}))
    gate_id = _exhaust(fixture, dispatcher)
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=fixture["registry"])
    # Without an option, approve selects the default option (raise_budget).
    choice = {"option": option} if option else {"decision": "approve"}
    proposal = service.preview({
        "action_kind": "gate.resolve", "summary": "raise the budget",
        "normalized_parameters": {"goal_id": GOAL_ID, "todo_id": gate_id, **choice, "note": "$0.2"},
        "context": {}, "idempotency_key": "budget-raise",
    })
    assert proposal["status"] == "preview_ready", proposal
    return fixture, gate_id, service, proposal


def _lose_receipt(service: ChatActionService, proposal_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        def lost_response(*args: Any, **kwargs: Any) -> None:
            raise ConnectionError("Synthetic receipt loss after the gate closed")
        patch.setattr(service.store, "apply", lost_response)
        with pytest.raises(ConnectionError):
            service.apply(proposal_id)


@pytest.mark.parametrize("option", ["raise_budget", None])
def test_a_lost_web_receipt_recovers_the_recorded_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str | None,
) -> None:
    fixture, gate_id, service, proposal = _raise_budget_preview(tmp_path, option=option)
    _lose_receipt(service, proposal["proposal_id"], monkeypatch)
    entry = read_gate_index(fixture["runtime"], GOAL_ID)["gates"][gate_id]
    assert entry["budget_outcome"]["option"] == "raise_budget", "the omitted option was defaulted"
    recovered = service.apply(proposal["proposal_id"])["proposal"]
    assert recovered["status"] == "applied", recovered
    assert recovered["receipt"].get("decision_option") == option
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == 0.2


def test_a_crash_between_the_gate_closure_and_its_settlement_settles_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import loopx.plan_cards as plan_cards

    fixture, gate_id, service, proposal = _raise_budget_preview(tmp_path)
    with monkeypatch.context() as patch:
        def crash(**kwargs: Any) -> None:
            raise RuntimeError("Synthetic crash after the gate closed, before its settlement")
        patch.setattr(plan_cards, "settle_gate_decision", crash)
        with pytest.raises(RuntimeError):
            service.apply(proposal["proposal_id"])
    assert _budget_gates(fixture) == [], "the gate closed"
    assert "budget_outcome" not in read_gate_index(fixture["runtime"], GOAL_ID)["gates"][gate_id]
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == 0.05
    settled = service.apply(proposal["proposal_id"])["proposal"]
    assert settled["status"] == "applied", settled
    entry = read_gate_index(fixture["runtime"], GOAL_ID)["gates"][gate_id]
    assert entry["budget_outcome"]["option"] == "raise_budget" and entry["budget_outcome"]["ok"] is True
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == 0.2
    assert len(_events(fixture, "usage_budget_decided")) == 1


def test_a_second_settlement_replays_the_first_option(tmp_path: Path) -> None:
    fixture, gate_id, service, proposal = _raise_budget_preview(tmp_path)
    assert service.apply(proposal["proposal_id"])["proposal"]["status"] == "applied"
    replayed = settle_budget_gate(
        registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID,
        gate_todo_id=gate_id, decision="reject", option="stop_goal", note=None,
    )
    assert replayed["replayed"] is True and replayed["option"] == "raise_budget"
    assert read_gate_index(fixture["runtime"], GOAL_ID)["gates"][gate_id]["decision_option"] == "raise_budget"
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == 0.2
    assert not goal_is_stopped(load_goal_from_registry(fixture["registry"], GOAL_ID))
    assert len(_events(fixture, "usage_budget_decided")) == 1


def test_a_web_preview_is_stale_after_the_cli_picks_another_option(tmp_path: Path) -> None:
    # Same decision (approve), same note, different option: the web apply must not
    # report the previewed raise_budget as the recorded outcome.
    fixture, gate_id, service, proposal = _raise_budget_preview(tmp_path)
    code, payload = _cli(fixture, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", gate_id,
                         "--option", "continue_without_limit", "--note", "$0.2")
    assert code == 0 and payload["decision_outcome"] == "approve", payload
    stale = service.apply(proposal["proposal_id"])["proposal"]
    assert stale["status"] == "stale", stale
    assert not stale.get("receipt")
    entry = read_gate_index(fixture["runtime"], GOAL_ID)["gates"][gate_id]
    assert entry["budget_outcome"]["option"] == "continue_without_limit"
    assert read_usage_budget(fixture["runtime"], GOAL_ID) is None


def test_concurrent_settlements_apply_one_effect_and_the_loser_replays_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One settler raises the budget while another stops the goal, at the same time.
    import threading

    import loopx.usage_budget_gate as budget_gate

    fixture = _fixture(tmp_path)
    gate_id = _exhaust(fixture, _dispatcher(fixture, GoalShouldRun({})))
    effects: list[str] = []
    rendezvous = threading.Barrier(2, timeout=2)  # both effects in flight at once, if the code lets them

    def effect(name: str, real: Any) -> Any:
        def run(*args: Any, **kwargs: Any) -> Any:
            effects.append(name)
            with contextlib.suppress(threading.BrokenBarrierError):
                rendezvous.wait()
            return real(*args, **kwargs)
        return run

    monkeypatch.setattr(budget_gate, "write_usage_budget", effect("raise", budget_gate.write_usage_budget))
    monkeypatch.setattr(budget_gate, "_stop_goal", effect("stop", budget_gate._stop_goal))
    start = threading.Barrier(2, timeout=10)
    results: dict[str, Any] = {}

    def settle(name: str, decision: str, option: str, note: str | None) -> None:
        start.wait()
        try:
            results[name] = settle_budget_gate(
                registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID,
                gate_todo_id=gate_id, decision=decision, option=option, note=note,
            )
        except BaseException as error:  # surfaced below
            results[name] = error

    threads = [threading.Thread(target=settle, args=("raise", "approve", "raise_budget", "$0.2")),
               threading.Thread(target=settle, args=("stop", "reject", "stop_goal", None))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert all(isinstance(result, dict) for result in results.values()), results
    assert len(effects) == 1, f"only one effect may apply, applied: {effects}"
    [winner] = effects
    option = {"raise": "raise_budget", "stop": "stop_goal"}[winner]
    assert {result["option"] for result in results.values()} == {option}
    assert sorted(bool(result.get("replayed")) for result in results.values()) == [False, True]
    assert (read_usage_budget(fixture["runtime"], GOAL_ID) == 0.2) is (winner == "raise")
    assert goal_is_stopped(load_goal_from_registry(fixture["registry"], GOAL_ID)) is (winner == "stop")
    assert read_gate_index(fixture["runtime"], GOAL_ID)["gates"][gate_id]["budget_outcome"]["option"] == option
    assert len(_events(fixture, "usage_budget_decided")) == 1


def _break_budget_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    import loopx.usage_budget_gate as budget_gate

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise OSError("Synthetic budget store failure")

    monkeypatch.setattr(budget_gate, "write_usage_budget", unavailable)


def test_a_failed_budget_effect_is_surfaced_and_a_web_retry_applies_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, gate_id, service, proposal = _raise_budget_preview(tmp_path)
    with monkeypatch.context() as patch:
        _break_budget_writes(patch)
        failed = service.apply(proposal["proposal_id"])["proposal"]
    assert failed["status"] == "failed", failed
    failure = failed["failure"]
    assert failure["error_code"] == "gate_settlement_retry_required", failure
    assert failure["details"]["gate_todo_id"] == gate_id
    assert (failure["details"]["settlement"], failure["details"]["option"]) == ("budget_gate", "raise_budget")
    assert _budget_gates(fixture) == [], "the gate decision is recorded"
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == 0.05
    assert read_gate_index(fixture["runtime"], GOAL_ID)["gates"][gate_id]["budget_outcome"]["ok"] is False

    retried = service.apply(proposal["proposal_id"])["proposal"]
    assert retried["status"] == "applied", retried
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == 0.2
    outcome = read_gate_index(fixture["runtime"], GOAL_ID)["gates"][gate_id]["budget_outcome"]
    assert (outcome["ok"], outcome["option"]) == (True, "raise_budget")
    assert [event["details"]["ok"] for event in _events(fixture, "usage_budget_decided")] == [False, True]
    assert service.apply(proposal["proposal_id"])["proposal"]["receipt"] == retried["receipt"]


def test_a_failed_budget_effect_is_retried_by_a_later_cli_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    gate_id = _exhaust(fixture, _dispatcher(fixture, GoalShouldRun({})))
    with monkeypatch.context() as patch:
        _break_budget_writes(patch)
        code, payload = _cli(fixture, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", gate_id,
                             "--option", "raise_budget", "--note", "$0.2")
    assert code == 0 and payload["budget_gate"]["ok"] is False, payload
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == 0.05
    # The same decision settled again (a `todo complete` replay) retries the recorded option.
    code, payload = _cli(fixture, "todo", "complete", "--goal-id", GOAL_ID, "--todo-id", gate_id, "--role", "user",
                         "--decision-outcome", "approve", "--agent-id", "orch", "--note", "$0.2")
    assert code == 0, payload
    assert payload["budget_gate"]["ok"] is True and payload["budget_gate"]["option"] == "raise_budget", payload
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == 0.2


def _interrupt_outcome_record(patch: pytest.MonkeyPatch, outcome_key: str) -> None:
    """Fail the durable record of a settlement's outcome (its effect has already run)."""

    import loopx.gate_threads as gate_threads

    real = gate_threads.mark_gate_closed

    def record(*args: Any, **kwargs: Any) -> Any:
        outcome = (kwargs.get("extra") or {}).get(outcome_key)
        if isinstance(outcome, dict) and outcome.get("state") != "applying":
            raise OSError("Synthetic failure recording the settlement outcome")
        return real(*args, **kwargs)

    patch.setattr(gate_threads, "mark_gate_closed", record)


def test_an_interrupted_settlement_is_retried_with_its_pinned_option_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    gate_id = _exhaust(fixture, _dispatcher(fixture, GoalShouldRun({})))

    def settle(option: str | None) -> dict[str, Any]:
        return settle_budget_gate(registry_path=fixture["registry"], runtime_root=fixture["runtime"],
                                  goal_id=GOAL_ID, gate_todo_id=gate_id, decision="approve", option=option,
                                  note=None)

    raised = default_raised_budget(0.05)  # approve without an option raises the budget by half
    assert default_raised_budget(raised) != raised
    with monkeypatch.context() as patch:
        _interrupt_outcome_record(patch, "budget_outcome")
        with pytest.raises(OSError):
            settle(None)
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == raised, "the effect ran"

    # The same approve with the other approve option: the pinned choice and target win.
    retried = settle("continue_without_limit")
    assert (retried["ok"], retried["option"]) == (True, "raise_budget"), retried
    assert not retried.get("replayed")
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == raised, "neither cleared nor raised twice"
    entry = read_gate_index(fixture["runtime"], GOAL_ID)["gates"][gate_id]
    assert entry["decision_option"] == "raise_budget"
    assert (entry["budget_outcome"]["budget_usd"], entry["budget_outcome"]["ok"]) == (raised, True)
    assert settle("continue_without_limit")["replayed"] is True
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == raised


@pytest.mark.parametrize("failure", ["outcome_record", "settlement_lock_timeout"])
def test_a_settlement_that_cannot_run_or_be_recorded_leaves_the_proposal_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    import loopx.gate_threads as gate_threads
    from loopx.file_lock import exclusive_file_lock

    fixture, gate_id, service, proposal = _raise_budget_preview(tmp_path)
    proposal_id = proposal["proposal_id"]
    with monkeypatch.context() as patch:
        if failure == "outcome_record":
            _interrupt_outcome_record(patch, "budget_outcome")
            failed = service.apply(proposal_id)["proposal"]
        else:  # another settlement of this gate holds its lock past the timeout
            patch.setattr(gate_threads, "GATE_SETTLEMENT_LOCK_TIMEOUT_SECONDS", 0.0)
            with exclusive_file_lock(gate_threads.gate_settlement_lock_path(fixture["runtime"], GOAL_ID, gate_id)):
                failed = service.apply(proposal_id)["proposal"]
    assert failed["status"] == "failed", failed
    assert failed["failure"]["error_code"] == "gate_settlement_retry_required", failed["failure"]
    assert failed["failure"]["details"]["operation_id"] == f"chat-gate:{proposal_id}"
    assert _budget_gates(fixture) == [], "the gate decision is recorded"

    retried = service.apply(proposal_id)["proposal"]
    assert retried["status"] == "applied", retried
    assert read_usage_budget(fixture["runtime"], GOAL_ID) == 0.2
    outcome = read_gate_index(fixture["runtime"], GOAL_ID)["gates"][gate_id]["budget_outcome"]
    assert (outcome["ok"], outcome["option"]) == (True, "raise_budget")
