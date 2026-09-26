"""Fork gap G2: orchestrator-owned per-todo acceptance criteria.

Plan acceptance lands in the dedicated ``acceptance_criteria`` todo field,
the developer's delivery (which overwrites the note) never touches it, only
the orchestrator (or the owner) may write it under role_v1, and developer and
acceptor Turns see it together with the goal acceptance contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loopx.cli import main
from loopx.control_plane.status.role_board_projection import build_goal_role_board
from loopx.control_plane.todos.contract import TODO_ACCEPTANCE_CRITERIA_LIMIT
from loopx.control_plane.turn_driver.codex_cli import _prompt
from loopx.dispatch.prompts import dispatch_prompt_addendum
from loopx.event_sourced_state import (
    backfill_todo_events_from_markdown,
    build_state_projection,
    make_state_event,
)
from loopx.rollout_event_log import load_rollout_events, rollout_event_log_path
from loopx.todo_acceptance_criteria import (
    AcceptanceCriteriaAuthorError,
    set_goal_todo_acceptance_criteria,
)
from loopx.todos import add_goal_todo, complete_goal_todo, list_goal_todos, update_goal_todo

from test_gates_plans_intake import ACC, DEV, GOAL, ORCH, PLAN, fixture, rows
from test_role_v1_acceptance import (
    DEV as ROLE_DEV,
    ORCH as ROLE_ORCH,
    ACC as ROLE_ACC,
    _api,
    _setup,
    _todo,
)

CRITERIA = "GET /orders returns 200 with a JSON list; the orders tests pass"


def _approve_plan(registry: Path, runtime: Path, plan_body: dict = PLAN) -> dict[str, str]:
    from loopx.plan_cards import propose_plan

    plan = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH,
                        plan=plan_body)["plan"]
    done = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=plan["gate_todo_id"], role="user",
                              decision_outcome="approve", note="Go", no_followup=True, agent_id=ORCH)
    assert done["plan_card"]["status"] == "applied", done
    return done["plan_card"]["todo_id_map"]


# --- writers -----------------------------------------------------------------


DELIVERY_PLAN = {
    "title": "Orders", "summary": "One item.",
    "todos": [{"key": "orders", "text": "Implement the orders endpoint", "bound_agent": DEV,
               "acceptor_agent": ACC, "acceptance": CRITERIA, "estimated_effort": "1h"}],
}


@pytest.mark.parametrize("provider", [None, "file"])
def test_plan_apply_stores_the_criteria_and_delivery_keeps_them(tmp_path: Path, provider: str | None) -> None:
    registry, runtime = fixture(tmp_path, provider)
    todo_id = _approve_plan(registry, runtime, DELIVERY_PLAN)["orders"]
    todo = rows(registry)[todo_id]
    assert todo["acceptance_criteria"] == CRITERIA
    assert "Acceptance" not in todo["note"] and "Estimate: 1h" in todo["note"]

    # The developer's delivery overwrites the note with its next_action (as the
    # Turn writeback does); the criteria survive into review.
    delivered = complete_goal_todo(
        registry_path=registry, goal_id=GOAL, todo_id=todo_id, role="agent", agent_id=DEV,
        evidence="endpoint implemented", note="next: paginate",
    )
    assert delivered.get("in_review") is True, delivered
    reviewed = rows(registry)[todo_id]
    assert reviewed["status"] == "in_review"
    assert reviewed["note"] == "next: paginate"
    assert reviewed["acceptance_criteria"] == CRITERIA


@pytest.mark.parametrize("promoted", [False, True])
def test_orchestrator_writes_criteria_on_a_claimed_todo(tmp_path: Path, promoted: bool) -> None:
    registry, runtime = _setup(tmp_path, promoted=promoted)
    result = set_goal_todo_acceptance_criteria(
        **_api(registry, runtime), todo_id="todo_orders_api", acceptance_criteria=CRITERIA, agent_id=ROLE_ORCH,
    )
    change = result["acceptance_criteria_change"]
    assert (change["author"], change["lifecycle_actor"], change["change_class"]) == (ROLE_ORCH, ROLE_DEV, "major")
    assert change["changed"] is True and change["previous_sha256"] is None and len(change["sha256"]) == 64
    todo = _todo(registry, runtime, "todo_orders_api")
    assert todo["acceptance_criteria"] == CRITERIA and todo["claimed_by"] == ROLE_DEV

    # Delivery and the reject verdict leave the criteria alone.
    complete_goal_todo(**_api(registry, runtime), todo_id="todo_orders_api", role="agent", agent_id=ROLE_DEV,
                       evidence="endpoint implemented", note="next: paginate")
    from loopx.todo_acceptance import reject_goal_todo

    reject_goal_todo(**_api(registry, runtime), todo_id="todo_orders_api", agent_id=ROLE_ACC,
                     note="criterion 1 fails: the list is not JSON")
    todo = _todo(registry, runtime, "todo_orders_api")
    assert todo["status"] == "open" and todo["acceptance_criteria"] == CRITERIA

    cleared = set_goal_todo_acceptance_criteria(
        **_api(registry, runtime), todo_id="todo_orders_api", acceptance_criteria=None, agent_id=ROLE_ORCH,
    )
    assert cleared["acceptance_criteria_change"]["sha256"] is None
    assert "acceptance_criteria" not in _todo(registry, runtime, "todo_orders_api")


@pytest.mark.parametrize("promoted", [False, True])
@pytest.mark.parametrize("agent", [ROLE_DEV, ROLE_ACC])
def test_non_orchestrator_writes_are_rejected(tmp_path: Path, promoted: bool, agent: str) -> None:
    registry, runtime = _setup(tmp_path, promoted=promoted)
    with pytest.raises(AcceptanceCriteriaAuthorError, match="only the goal orchestrator"):
        set_goal_todo_acceptance_criteria(
            **_api(registry, runtime), todo_id="todo_orders_api", acceptance_criteria="lower the bar", agent_id=agent,
        )
    with pytest.raises(AcceptanceCriteriaAuthorError):
        update_goal_todo(**_api(registry, runtime), todo_id="todo_orders_api", agent_id=agent,
                         role_contract={"acceptance_criteria": "lower the bar"})
    with pytest.raises(AcceptanceCriteriaAuthorError):
        add_goal_todo(**_api(registry, runtime), role="agent", text="Self-accepting work", agent_id=agent,
                      role_contract={"acceptance_criteria": "anything goes"})
    assert "acceptance_criteria" not in _todo(registry, runtime, "todo_orders_api")
    # The owner (no agent id) and the orchestrator may create todos with criteria.
    for author in (None, ROLE_ORCH):
        added = add_goal_todo(**_api(registry, runtime), role="agent", text=f"Criteria work {author}",
                              agent_id=author, role_contract={"acceptance_criteria": "a; b"})
        assert _todo(registry, runtime, added["todo_id"])["acceptance_criteria"] == "a; b"


def test_criteria_are_bounded_single_line(tmp_path: Path) -> None:
    registry, runtime = _setup(tmp_path, promoted=False)
    set_goal_todo_acceptance_criteria(
        **_api(registry, runtime), todo_id="todo_orders_api", agent_id=ROLE_ORCH,
        acceptance_criteria="line one\nline two " + "x" * 2000,
    )
    stored = _todo(registry, runtime, "todo_orders_api")["acceptance_criteria"]
    assert stored.startswith("line one line two") and "\n" not in stored
    assert len(stored) == TODO_ACCEPTANCE_CRITERIA_LIMIT and stored.endswith("...")


def test_peer_v1_goals_keep_their_behaviour(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path, agent_model="peer_v1")
    todo_id = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Peer work",
                            claimed_by=DEV)["todo_id"]
    # No role check on peer goals: the claim owner may write the field.
    update_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, agent_id=DEV,
                     role_contract={"acceptance_criteria": "peer criteria"})
    assert rows(registry)[todo_id]["acceptance_criteria"] == "peer criteria"
    # And a todo without the field reads as before.
    plain = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Plain work")["todo_id"]
    assert "acceptance_criteria" not in rows(registry)[plain]


# --- CLI -----------------------------------------------------------------------


def test_cli_update_records_a_major_change_and_refuses_other_writers(tmp_path: Path, capsys) -> None:
    registry, runtime = _setup(tmp_path, promoted=False)
    base = ["--registry", str(registry), "--runtime-root", str(runtime), "--format", "json", "todo", "update",
            "--goal-id", "goal-acc", "--todo-id", "todo_orders_api"]
    assert main([*base, "--agent-id", ROLE_DEV, "--acceptance-criteria", "lower the bar"]) == 1
    assert "only the goal orchestrator" in json.loads(capsys.readouterr().out)["error"]
    assert main([*base, "--agent-id", ROLE_ORCH, "--acceptance-criteria", CRITERIA, "--note", "x"]) == 1
    assert "without other fields" in json.loads(capsys.readouterr().out)["error"]

    assert main([*base, "--agent-id", ROLE_ORCH, "--acceptance-criteria", CRITERIA]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["acceptance_criteria_change"]["change_class"] == "major"
    assert _todo(registry, runtime, "todo_orders_api")["acceptance_criteria"] == CRITERIA
    events = [event for event in load_rollout_events(rollout_event_log_path(runtime, "goal-acc"))
              if event["event_kind"] == "todo_update"]
    details = events[-1]["details"]
    assert events[-1]["agent_id"] == ROLE_ORCH
    assert details["acceptance_criteria_changed"] is True
    assert details["acceptance_criteria_change_class"] == "major"
    assert details["acceptance_criteria_author"] == ROLE_ORCH
    assert details["acceptance_criteria_sha256"] == payload["acceptance_criteria_change"]["sha256"]
    assert CRITERIA not in json.dumps(events)  # digests only, never the text

    assert main([*base, "--agent-id", ROLE_ORCH, "--clear-acceptance-criteria"]) == 0
    capsys.readouterr()
    assert "acceptance_criteria" not in _todo(registry, runtime, "todo_orders_api")


def test_cli_rework_instructions_go_to_review_feedback(tmp_path: Path, capsys) -> None:
    """G10: the orchestrator's rework instructions land in review_feedback, not the note."""

    registry, runtime = _setup(tmp_path, promoted=False)
    base = ["--registry", str(registry), "--runtime-root", str(runtime), "--format", "json", "todo", "update",
            "--goal-id", "goal-acc", "--todo-id", "todo_orders_api", "--agent-id", ROLE_DEV]
    assert main([*base, "--status", "open", "--reject-count", "0",
                 "--review-feedback", "Return the list as JSON; keep the endpoint"]) == 0
    capsys.readouterr()
    todo = _todo(registry, runtime, "todo_orders_api")
    assert todo["review_feedback"] == "Return the list as JSON; keep the endpoint"
    assert not todo.get("note")
    assert main([*base, "--clear-review-feedback"]) == 0
    capsys.readouterr()
    assert "review_feedback" not in _todo(registry, runtime, "todo_orders_api")


# --- readers -------------------------------------------------------------------


def _request(selected: dict) -> dict:
    return {
        "schema_version": "loopx_turn_host_request_v0", "turn_key": "sha256:" + "a" * 64,
        "route": "ready_for_host",
        "turn_envelope": {"schema_version": "loopx_turn_envelope_v0", "goal_id": "g", "agent_id": "a",
                          "action": {"selected_todo": {"todo_id": "todo_fixture0001", "text": "Build", **selected}}},
    }


GOAL_ACCEPTANCE = {"objective": "Ship orders", "criteria": ["c1: API documented", "c2: tests pass"]}


def test_envelope_carries_the_criteria_and_the_goal_contract() -> None:
    from test_turn_envelope import _full_decision
    from loopx.control_plane.quota.turn_envelope import build_turn_envelope

    source = _full_decision()
    source["selected_todo"].update({"acceptance_criteria": CRITERIA + " " + "y" * 1200,
                                    "goal_acceptance": GOAL_ACCEPTANCE})
    envelope = build_turn_envelope(source)
    selected = envelope["action"]["selected_todo"]
    assert selected["acceptance_criteria"].startswith(CRITERIA) and len(selected["acceptance_criteria"]) <= 1000
    assert selected["goal_acceptance"] == GOAL_ACCEPTANCE
    assert envelope["action_signature"]["matches"] is True
    plain = _full_decision()
    assert "acceptance_criteria" not in build_turn_envelope(plain)["action"]["selected_todo"]


def test_selected_todo_projection_keeps_the_criteria() -> None:
    from loopx.control_plane.quota.selected_todo_projection import selected_todo_projection

    selected = selected_todo_projection(
        agent_lane_next_action={"todo_id": "todo_fixture0001", "text": "Build", "status": "open",
                                "task_class": "advancement_task", "acceptance_criteria": CRITERIA},
        work_lane_contract=None,
    )
    assert selected["acceptance_criteria"] == CRITERIA


def test_acceptor_and_developer_prompts_show_the_criteria() -> None:
    review = _prompt(_request({"status": "in_review", "acceptance_criteria": CRITERIA,
                               "goal_acceptance": GOAL_ACCEPTANCE}))
    assert CRITERIA in review and "c1: API documented" in review and "Ship orders" in review
    assert "Check each acceptance criterion" in review and "name in summary each criterion that failed" in review
    developer = _prompt(_request({"status": "open", "acceptance_criteria": CRITERIA}))
    assert CRITERIA in developer and "meet every criterion before delivering" in developer
    assert "Todo acceptance criteria" not in _prompt(_request({"status": "open"}))


def test_dispatcher_prompts_route_criteria_and_rework() -> None:
    kwargs = {"goal_id": "g", "todo_id": "todo_x", "workspace_repos": None}
    acceptor = dispatch_prompt_addendum(agent_id="acc", role="acceptor", **kwargs)
    assert "acceptance_criteria" in acceptor and "goal acceptance contract" in acceptor
    assert "each criterion that failed" in acceptor
    orchestrator = dispatch_prompt_addendum(agent_id="orch", role="orchestrator", **kwargs)
    assert "--review-feedback" in orchestrator and "never in the note" in orchestrator
    assert "--acceptance-criteria" in orchestrator and "major change" in orchestrator
    assert "--note ...`" not in orchestrator


@pytest.mark.parametrize("promoted", [False, True])
def test_turn_decision_reads_the_durable_criteria(tmp_path: Path, promoted: bool, monkeypatch) -> None:
    from loopx import todos as todos_module
    from loopx.cli_commands.turn_decision import _with_durable_todo_note

    registry, runtime = _setup(tmp_path, promoted=promoted)
    set_goal_todo_acceptance_criteria(**_api(registry, runtime), todo_id="todo_orders_api",
                                      acceptance_criteria=CRITERIA, agent_id=ROLE_ORCH)

    def build(**_kwargs):
        return {"selected_todo": {"todo_id": "todo_orders_api"}}

    wrapped = _with_durable_todo_note(build, registry_path=registry, runtime_root=runtime, goal_id="goal-acc")
    assert wrapped()["selected_todo"]["acceptance_criteria"] == CRITERIA

    # An enabled goal acceptance contract (canonical goals) is attached as a bounded brief.
    real_list = todos_module.list_goal_todos

    def listed(**kwargs):
        result = real_list(**kwargs)
        result["agent_todos"] = {**(result.get("agent_todos") or {}), "goal_acceptance_contract": {
            "enabled": True, "objective": "Ship orders",
            "criteria": [{"id": "c1", "description": "API documented"}, {"id": "c2", "description": "tests pass"}]}}
        return result

    monkeypatch.setattr(todos_module, "list_goal_todos", listed)
    selected = wrapped()["selected_todo"]
    assert selected["goal_acceptance"] == GOAL_ACCEPTANCE


# --- event log, replay and projections -------------------------------------------


def test_markdown_backfill_and_replay_keep_the_criteria() -> None:
    state = (
        "# Goal\n\n## User Todo\n\n## Agent Todo\n- [ ] [P1] Implement the orders API.\n"
        "  <!-- loopx:todo todo_id=todo_orders_api role=agent task_class=advancement_task status=open "
        "acceptance_criteria=GET%20/orders%20returns%20200 -->\n"
    )
    events = backfill_todo_events_from_markdown(state, goal_id="goal-acc", recorded_at="2026-09-26T00:00:01Z")
    added = next(event for event in events if event["event_type"] == "todo_added")
    assert added["payload"]["acceptance_criteria"] == "GET /orders returns 200"
    item = build_state_projection(events, goal_id="goal-acc")["agent_todos"]["items"][0]
    assert item["acceptance_criteria"] == "GET /orders returns 200"

    update = make_state_event(
        event_id="evt-criteria", goal_id="goal-acc", event_type="todo_updated",
        refs={"todo_id": "todo_orders_api"}, payload={"acceptance_criteria": "revised criteria"},
        recorded_at="2026-09-26T00:00:09Z", producer="test",
    )
    item = build_state_projection([*events, update], goal_id="goal-acc")["agent_todos"]["items"][0]
    assert item["acceptance_criteria"] == "revised criteria"


def test_old_events_without_the_field_replay_as_before() -> None:
    event = make_state_event(
        event_id="evt-1", goal_id="goal-acc", event_type="todo_added", refs={"todo_id": "todo_old"},
        payload={"text": "Old work", "role": "agent"}, recorded_at="2026-09-26T00:00:01Z", producer="test",
    )
    item = build_state_projection([event], goal_id="goal-acc")["agent_todos"]["items"][0]
    assert "acceptance_criteria" not in item


def test_status_list_and_role_board_expose_the_criteria(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    ids = _approve_plan(registry, runtime)
    listed = list_goal_todos(registry_path=registry, goal_id=GOAL)["todos"]
    goal = json.loads(registry.read_text())["goals"][0]
    board = build_goal_role_board(goal=goal, todos=listed, runtime_root=runtime, dispatcher={"available": False})
    cards = {card["todo_id"]: card for card in board["todos"]}
    assert cards[ids["contract"]]["acceptance_criteria"] == "openapi covers CRUD"
    assert "acceptance_criteria" not in cards[ids["api"]]
    assert cards[ids["contract"]]["acceptor_agent"] == ACC
