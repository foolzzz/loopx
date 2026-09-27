"""Fork design decision 40: acceptance-criteria changes need a user-approved plan card.

After initial planning an agent cannot change a todo's acceptance_criteria
directly under role_v1; the orchestrator proposes a plan card with
``criteria_changes``. Approve applies (stale entries are refused), reject and
cancel change nothing and wake the orchestrator, and the acceptor does not
review the todo while the card is pending.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loopx.chat_action_store import ChatActionStore
from loopx.chat_actions import ChatActionService
from loopx.cli import main
from loopx.control_plane.agents.identity import build_quota_agent_identity
from loopx.control_plane.quota.should_run_prepare import _with_role_v1_lane_facts
from loopx.control_plane.status.role_board_projection import build_goal_role_board
from loopx.control_plane.todos.quota_summary import select_quota_todo_summary
from loopx.dispatch.orchestrator_actions import is_orchestrator_action_todo
from loopx.gate_threads import gate_view, render_gate_markdown
from loopx.plan_cards import PlanCardError, apply_plan, propose_plan, read_plan, render_plan_markdown
from loopx.plan_criteria_changes import criteria_change_pending_todo_ids
from loopx.rollout_event_log import load_rollout_events, rollout_event_log_path
from loopx.todo_acceptance_criteria import AcceptanceCriteriaAuthorError, set_goal_todo_acceptance_criteria
from loopx.todos import complete_goal_todo, list_goal_todos, update_goal_todo

from test_gates_plans_intake import ACC, DEV, GOAL, ORCH, fixture, rows

OLD_CRITERIA = "GET /orders returns 200 with a JSON list"
NEW_CRITERIA = "GET /orders returns 200 with a JSON list paged by 50"
INITIAL_PLAN = {
    "title": "Orders", "summary": "One item.",
    "todos": [{"key": "orders", "text": "Implement the orders endpoint", "bound_agent": DEV,
               "acceptor_agent": ACC, "acceptance": OLD_CRITERIA}],
}


def _initial(registry: Path, runtime: Path) -> str:
    plan = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH,
                        plan=INITIAL_PLAN)["plan"]
    done = _decide(registry, plan["gate_todo_id"], "approve")
    todo_id = done["plan_card"]["todo_id_map"]["orders"]
    # Initial plan apply writes the (user-approved) criteria directly.
    assert rows(registry)[todo_id]["acceptance_criteria"] == OLD_CRITERIA
    return todo_id


def _change_plan(todo_id: str, new: str | None = NEW_CRITERIA, **extra) -> dict:
    return {"title": "Page the orders list",
            "criteria_changes": [{"todo_id": todo_id, "new": new, "reason": "the user asked for paging", **extra}]}


def _propose_change(registry: Path, runtime: Path, todo_id: str, **extra) -> dict:
    return propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH,
                        plan=_change_plan(todo_id, **extra))["plan"]


def _decide(registry: Path, gate_id: str, decision: str, note: str = "ok") -> dict:
    done = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=gate_id, role="user",
                              decision_outcome=decision, note=note, no_followup=True, agent_id=ORCH)
    assert done["ok"] is True, done
    return done


def _events(runtime: Path, kind: str) -> list[dict]:
    return [event for event in load_rollout_events(rollout_event_log_path(runtime, GOAL))
            if event["event_kind"] == kind]


def _orchestrator_notices(registry: Path) -> list[dict]:
    return [row for row in rows(registry).values()
            if is_orchestrator_action_todo(row) and "acceptance-criteria change" in row["text"]]


# --- direct edits ---------------------------------------------------------------


@pytest.mark.parametrize("provider", [None, "file"])
def test_a_direct_orchestrator_edit_is_refused(tmp_path: Path, provider: str | None, capsys) -> None:
    registry, runtime = fixture(tmp_path, provider)
    todo_id = _initial(registry, runtime)
    with pytest.raises(AcceptanceCriteriaAuthorError, match="user-approved plan card") as refused:
        set_goal_todo_acceptance_criteria(registry_path=registry, goal_id=GOAL, todo_id=todo_id,
                                          acceptance_criteria=NEW_CRITERIA, agent_id=ORCH)
    assert refused.value.code == "acceptance_criteria_change_requires_plan"
    assert f"loopx plan propose --goal-id {GOAL} --agent-id {ORCH}" in str(refused.value)
    # The library update path refuses every agent, the orchestrator and the claim owner alike.
    for agent in (ORCH, DEV):
        with pytest.raises(AcceptanceCriteriaAuthorError):
            update_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, agent_id=agent,
                             role_contract={"acceptance_criteria": NEW_CRITERIA})
    assert main(["--registry", str(registry), "--runtime-root", str(runtime), "--format", "json", "todo",
                 "update", "--goal-id", GOAL, "--todo-id", todo_id, "--agent-id", ORCH,
                 "--acceptance-criteria", NEW_CRITERIA]) == 1
    assert "criteria_changes" in json.loads(capsys.readouterr().out)["error"]
    assert rows(registry)[todo_id]["acceptance_criteria"] == OLD_CRITERIA


@pytest.mark.parametrize("provider", [None, "file"])
def test_an_owner_edit_is_allowed_and_logged(tmp_path: Path, provider: str | None) -> None:
    registry, runtime = fixture(tmp_path, provider)
    todo_id = _initial(registry, runtime)
    result = set_goal_todo_acceptance_criteria(registry_path=registry, goal_id=GOAL, todo_id=todo_id,
                                               acceptance_criteria=NEW_CRITERIA)
    change = result["acceptance_criteria_change"]
    assert (change["author"], change["source"], change["lifecycle_actor"], change["changed"]) == (
        None, "owner", DEV, True)
    assert rows(registry)[todo_id]["acceptance_criteria"] == NEW_CRITERIA


# --- the plan-card path ------------------------------------------------------------


@pytest.mark.parametrize("provider", [None, "file"])
def test_propose_then_approve_applies_the_change(tmp_path: Path, provider: str | None) -> None:
    registry, runtime = fixture(tmp_path, provider)
    todo_id = _initial(registry, runtime)
    plan = _propose_change(registry, runtime, todo_id)
    assert plan["plan"]["todos"] == []
    assert plan["plan"]["criteria_changes"] == [
        {"todo_id": todo_id, "old": OLD_CRITERIA, "new": NEW_CRITERIA, "reason": "the user asked for paging"}]
    gate = rows(registry)[plan["gate_todo_id"]]
    assert "1 acceptance-criteria change" in gate["text"]
    # Nothing changes before the user decides.
    assert rows(registry)[todo_id]["acceptance_criteria"] == OLD_CRITERIA
    assert criteria_change_pending_todo_ids(runtime, GOAL) == [todo_id]

    # plan show and gate show carry old and new side by side.
    shown = render_plan_markdown({"ok": True, "plan": read_plan(runtime, GOAL, plan["plan_id"])})
    assert f"| `{todo_id}` | {OLD_CRITERIA} | {NEW_CRITERIA} | the user asked for paging |" in shown
    view = gate_view(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=plan["gate_todo_id"])
    assert view["criteria_changes"] == [
        {"todo_id": todo_id, "old": OLD_CRITERIA, "new": NEW_CRITERIA, "reason": "the user asked for paging"}]
    assert "## Acceptance-criteria changes" in render_gate_markdown(view)

    done = _decide(registry, plan["gate_todo_id"], "approve")
    assert done["plan_card"]["status"] == "applied"
    assert [item["status"] for item in done["plan_card"]["criteria_change_results"]] == ["applied"]
    assert rows(registry)[todo_id]["acceptance_criteria"] == NEW_CRITERIA
    assert criteria_change_pending_todo_ids(runtime, GOAL) == []
    [event] = _events(runtime, "todo_criteria_change")
    assert (event["todo_id"], event["status"], event["agent_id"]) == (todo_id, "applied", ORCH)
    assert event["details"]["plan_id"] == plan["plan_id"] and event["details"]["approved_by"] == "user"
    assert len(event["details"]["sha256"]) == 64 and len(event["details"]["previous_sha256"]) == 64
    assert NEW_CRITERIA not in json.dumps(event)  # digests only
    assert not _orchestrator_notices(registry)
    # Applied exactly once.
    again = apply_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, plan_id=plan["plan_id"])
    assert again["already_applied"] is True and len(_events(runtime, "todo_criteria_change")) == 1


@pytest.mark.parametrize("decision", ["reject", "cancel"])
def test_reject_or_cancel_leaves_the_criteria_and_wakes_the_orchestrator(tmp_path: Path, decision: str) -> None:
    registry, runtime = fixture(tmp_path)
    todo_id = _initial(registry, runtime)
    plan = _propose_change(registry, runtime, todo_id)
    done = _decide(registry, plan["gate_todo_id"], decision, note="keep the current contract")
    assert done["plan_card"]["status"] == {"reject": "rejected", "cancel": "cancelled"}[decision]
    assert rows(registry)[todo_id]["acceptance_criteria"] == OLD_CRITERIA
    assert criteria_change_pending_todo_ids(runtime, GOAL) == []
    assert not _events(runtime, "todo_criteria_change")
    [notice] = _orchestrator_notices(registry)
    assert notice["claimed_by"] == ORCH and notice["required_role"] == "orchestrator"
    assert todo_id in notice["text"] and plan["gate_todo_id"] in notice["text"]
    assert "keep the current contract" in notice["note"]


@pytest.mark.parametrize("provider", [None, "file"])
def test_a_stale_proposal_is_refused_on_apply(tmp_path: Path, provider: str | None) -> None:
    registry, runtime = fixture(tmp_path, provider)
    todo_id = _initial(registry, runtime)
    batch = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH, plan={
        **_change_plan(todo_id),
        "todos": [{"key": "docs", "text": "Document the paging", "bound_agent": DEV, "acceptance": "README pages"}],
    })["plan"]
    # The owner edits the criteria after the proposal.
    set_goal_todo_acceptance_criteria(registry_path=registry, goal_id=GOAL, todo_id=todo_id,
                                      acceptance_criteria="owner rewrote the criteria")
    done = _decide(registry, batch["gate_todo_id"], "approve")
    [result] = done["plan_card"]["criteria_change_results"]
    assert result["status"] == "stale" and "stale" in result["error"]
    assert rows(registry)[todo_id]["acceptance_criteria"] == "owner rewrote the criteria"
    # The rest of the batch still applies.
    docs_id = done["plan_card"]["todo_id_map"]["docs"]
    assert rows(registry)[docs_id]["acceptance_criteria"] == "README pages"
    [event] = _events(runtime, "todo_criteria_change")
    assert event["status"] == "stale"
    [notice] = _orchestrator_notices(registry)
    assert "went stale" in notice["text"]


def test_propose_validates_the_change(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    todo_id = _initial(registry, runtime)
    with pytest.raises(PlanCardError, match="no longer match") as stale:
        _propose_change(registry, runtime, todo_id, old="some older criteria")
    assert stale.value.code == "criteria_change_stale"
    with pytest.raises(PlanCardError, match="does not change"):
        propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH,
                     plan=_change_plan(todo_id, new=OLD_CRITERIA))
    with pytest.raises(PlanCardError, match="unknown todo"):
        _propose_change(registry, runtime, "todo_doesnotexist")
    with pytest.raises(PlanCardError, match="unknown field"):
        propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH,
                     plan={"title": "x", "criteria_changes": [{"todo_id": todo_id, "new": "x", "reason": "y", "z": 1}]})
    with pytest.raises(PlanCardError, match="not the orchestrator"):
        propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=DEV,
                     plan=_change_plan(todo_id))
    # One pending change per todo; a matching `old` is accepted.
    first = _propose_change(registry, runtime, todo_id, old=OLD_CRITERIA)
    with pytest.raises(PlanCardError, match="already has a criteria change") as pending:
        propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH,
                     plan=_change_plan(todo_id, new="another rewrite"))
    assert pending.value.code == "criteria_change_already_pending"
    # Revising the pending card itself is fine, and clearing (new=null) is a change.
    revised = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH,
                           plan=_change_plan(todo_id, new=None), revise_plan_id=first["plan_id"])["plan"]
    assert revised["revision"] == 2 and revised["plan"]["criteria_changes"][0]["new"] is None
    _decide(registry, first["gate_todo_id"], "approve")
    assert "acceptance_criteria" not in rows(registry)[todo_id]


def test_the_web_gate_resolve_path_applies(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    todo_id = _initial(registry, runtime)
    plan = _propose_change(registry, runtime, todo_id)
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=registry)
    proposal = service.preview({"action_kind": "gate.resolve", "summary": "approve criteria", "context": {},
        "idempotency_key": "criteria-gate", "normalized_parameters": {
            "goal_id": GOAL, "todo_id": plan["gate_todo_id"], "decision": "approve", "note": "Page it"}})
    applied = service.apply(proposal["proposal_id"])["proposal"]
    assert applied["status"] == "applied", applied
    assert read_plan(runtime, GOAL, plan["plan_id"])["status"] == "applied"
    assert rows(registry)[todo_id]["acceptance_criteria"] == NEW_CRITERIA


# --- the acceptor waits while the card is pending --------------------------------------


def _acceptor_summary(registry: Path, runtime: Path) -> dict:
    goal = json.loads(registry.read_text())["goals"][0]
    listed = list_goal_todos(registry_path=registry, goal_id=GOAL, role="agent")
    identity = _with_role_v1_lane_facts(build_quota_agent_identity(goal, agent_id=ACC),
                                        runtime_root=str(runtime), goal_id=GOAL)
    return select_quota_todo_summary(listed["agent_todos"], None, agent_identity=identity)


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_the_acceptor_is_not_selected_while_the_card_is_pending(tmp_path: Path, decision: str) -> None:
    registry, runtime = fixture(tmp_path)
    todo_id = _initial(registry, runtime)
    other = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH, plan={
        "title": "More", "todos": [{"key": "other", "text": "Implement the users endpoint", "bound_agent": DEV,
                                    "acceptor_agent": ACC}]})["plan"]
    other_id = _decide(registry, other["gate_todo_id"], "approve")["plan_card"]["todo_id_map"]["other"]
    for delivered in (todo_id, other_id):
        assert complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=delivered, role="agent",
                                  agent_id=DEV, evidence="built").get("in_review") is True

    def executable() -> list[str]:
        return [item["todo_id"] for item in _acceptor_summary(registry, runtime)["first_executable_items"]]

    assert sorted(executable()) == sorted([todo_id, other_id])
    plan = _propose_change(registry, runtime, todo_id)
    # Held with a clear reason; the other delivered todo is still reviewed.
    assert executable() == [other_id]
    held = _acceptor_summary(registry, runtime)["role_scope"]["review_held"]
    assert held["reason"] == "criteria_change_pending" and held["count"] == 1
    assert [item["todo_id"] for item in held["items"]] == [todo_id]
    # The role board flags the card.
    goal = json.loads(registry.read_text())["goals"][0]
    board = build_goal_role_board(goal=goal, todos=list_goal_todos(registry_path=registry, goal_id=GOAL)["todos"],
                                  runtime_root=runtime, dispatcher={"available": False})
    card = next(card for card in board["todos"] if card["todo_id"] == todo_id)
    assert card["criteria_change_plan_id"] == plan["plan_id"]
    assert card["criteria_change_gate_todo_id"] == plan["gate_todo_id"]
    gate = next(gate for gate in board["gates"] if gate["todo_id"] == plan["gate_todo_id"])
    assert gate["plan_criteria_change_count"] == 1

    _decide(registry, plan["gate_todo_id"], decision)
    assert sorted(executable()) == sorted([todo_id, other_id])
    assert "review_held" not in _acceptor_summary(registry, runtime)["role_scope"]


def test_peer_v1_goals_are_unaffected(tmp_path: Path) -> None:
    from loopx.todos import add_goal_todo

    registry, runtime = fixture(tmp_path, agent_model="peer_v1")
    todo_id = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Peer work",
                            claimed_by=DEV, role_contract={"acceptance_criteria": OLD_CRITERIA})["todo_id"]
    # Any agent may still edit directly; no plan card is involved.
    result = set_goal_todo_acceptance_criteria(registry_path=registry, goal_id=GOAL, todo_id=todo_id,
                                               acceptance_criteria=NEW_CRITERIA, agent_id=DEV)
    assert result["acceptance_criteria_change"]["source"] == "agent"
    assert rows(registry)[todo_id]["acceptance_criteria"] == NEW_CRITERIA
    update_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, agent_id=DEV,
                     role_contract={"acceptance_criteria": "peer criteria"})
    assert rows(registry)[todo_id]["acceptance_criteria"] == "peer criteria"
    assert criteria_change_pending_todo_ids(runtime, GOAL) == []
