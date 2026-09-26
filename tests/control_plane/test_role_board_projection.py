"""Fork slice S8: the role board projection in the status payload."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from test_gates_plans_intake import ACC, DEV, GOAL, ORCH, PLAN, fixture, open_gate

from loopx.control_plane.status.role_board_projection import (
    MAX_ROLE_BOARD_OPEN_TODOS,
    ROLE_BOARD_SCHEMA_VERSION,
    build_goal_role_board,
)
from loopx.dispatch.state import empty_state, save_state
from loopx.gate_threads import reply_to_gate
from loopx.plan_cards import propose_plan
from loopx.status import collect_status
from loopx.todos import add_goal_todo, complete_goal_todo


def _status(registry: Path, runtime: Path) -> dict:
    return collect_status(registry_path=registry, runtime_root_override=str(runtime), scan_roots=[], limit=5)


def _board(payload: dict) -> dict:
    goal = next(goal for goal in payload["run_history"]["goals"] if goal["id"] == GOAL)
    return goal["role_board"]


def test_role_board_projects_roster_activity_todos_and_gates(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    # A plan approved by the user creates plan-linked todos.
    plan = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH, plan=PLAN)["plan"]
    applied = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=plan["gate_todo_id"], role="user",
                                 decision_outcome="approve", note="Go", no_followup=True, agent_id=ORCH)
    ids = applied["plan_card"]["todo_id_map"]
    rework = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Fix the login form",
                           claimed_by=DEV, role_contract={"reject_count": 2, "acceptor_agent": ACC,
                                                          "task_repositories": ["web"]})["todo_id"]
    running = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Wire the API client",
                            claimed_by=DEV)["todo_id"]
    finished = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Write docs",
                             role_contract={"requires_acceptance": False})["todo_id"]
    assert complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=finished, role="agent",
                              agent_id=DEV, evidence="docs written")["ok"]
    gate = open_gate(registry, "Which database?")
    reply_to_gate(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=gate, text="Why not Postgres?")

    # Dispatcher state: dev runs a live Turn, acc is unavailable, orch's provider cools down.
    now = time.time()
    state = empty_state()
    state["runs"] = {"r1": {"run_id": "r1", "goal_id": GOAL, "agent_id": DEV, "role": "developer",
                            "todo_id": running, "pid": os.getpid(), "started_at": now}}
    state["agent_slots"] = {ORCH: {"role": "orchestrator", "max": 1, "provider": "anthropic"},
                            DEV: {"role": "developer", "max": 2, "provider": "anthropic-dev"}}
    state["agent_cooldowns"] = {ACC: {"until": now + 300, "reason": "auth_preflight_failed", "status": "expired"}}
    state["provider_cooldowns"] = {"anthropic": {"until": now + 120, "kind": "rate_limited", "failures": 1}}
    save_state(runtime, state)

    board = _board(_status(registry, runtime))
    assert board["schema_version"] == ROLE_BOARD_SCHEMA_VERSION
    assert board["dispatcher"]["available"] is True

    agents = {agent["agent_id"]: agent for agent in board["agents"]}
    assert {agent_id: agent["role"] for agent_id, agent in agents.items()} == {
        ORCH: "orchestrator", DEV: "developer", ACC: "acceptor"}
    assert agents[DEV]["activity"] == "running" and agents[DEV]["running_todo_ids"] == [running]
    assert (agents[ACC]["activity"], agents[ACC]["reason"]) == ("unavailable", "expired")
    assert (agents[ORCH]["activity"], agents[ORCH]["reason"]) == ("cooldown", "rate_limited")

    cards = {card["todo_id"]: card for card in board["todos"]}
    assert all(card["todo_id"] != plan["gate_todo_id"] and card["todo_id"] != gate for card in board["todos"])
    contract = cards[ids["contract"]]
    assert (contract["status"], contract["claimed_by"], contract["acceptor_agent"]) == ("open", DEV, ACC)
    assert (contract["required_role"], contract["effective_role"]) == ("developer", "developer")
    assert contract["task_repositories"] == ["api"] and contract["requires_acceptance"] is True
    assert (contract["plan_id"], contract["plan_gate_todo_id"]) == (plan["plan_id"], plan["gate_todo_id"])
    assert cards[ids["api"]]["status"] == "deferred"
    assert cards[ids["readme"]]["requires_acceptance"] is False
    assert (cards[rework]["reject_count"], cards[rework]["task_repositories"]) == (2, ["web"])
    assert cards[running]["running"] is True and cards[running]["running_agent_id"] == DEV
    assert "running" not in cards[rework]
    assert cards[finished]["status"] == "done" and "running" not in cards[finished]

    # Only the still-open gate is listed; the answered plan gate is gone.
    assert [(g["todo_id"], g["kind"], g["awaiting"], g["message_count"]) for g in board["gates"]] == [
        (gate, "decision", "awaiting_orchestrator", 1)]


def test_role_board_lists_pending_plan_gate_first_and_degrades_without_dispatcher(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    decision = open_gate(registry, "Pick a CSS framework")
    reply_to_gate(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=decision, text="Tailwind?")
    plan = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH, plan=PLAN)["plan"]

    board = _board(_status(registry, runtime))
    assert board["dispatcher"] == {"available": False, "serving": False, "updated_at": None}
    assert {agent["activity"] for agent in board["agents"]} == {"unknown"}
    first = board["gates"][0]
    assert (first["todo_id"], first["kind"], first["awaiting"]) == (plan["gate_todo_id"], "plan_approval", "awaiting_user")
    assert (first["plan_id"], first["plan_status"], first["plan_title"], first["plan_todo_count"]) == (
        plan["plan_id"], "pending", "Todo app v1", 5)
    assert board["gates"][1]["todo_id"] == decision


def test_role_board_skips_peer_goals(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path, agent_model="peer_v1")
    goal = next(goal for goal in _status(registry, runtime)["run_history"]["goals"] if goal["id"] == GOAL)
    assert "role_board" not in goal


def test_role_board_passes_in_review_through_and_stays_bounded(tmp_path: Path) -> None:
    todos = [{"todo_id": f"todo_{index:012d}", "role": "agent", "status": "open", "text": "x" * 400,
              "task_class": "advancement_task"} for index in range(MAX_ROLE_BOARD_OPEN_TODOS + 5)]
    todos.append({"todo_id": "todo_review000001", "role": "agent", "status": "in_review", "text": "Review me",
                  "claimed_by": DEV, "acceptor_agent": ACC, "reject_count": 1})
    todos.extend({"todo_id": f"todo_done{index:08d}", "role": "agent", "status": "done", "done": True,
                  "text": "old", "updated_at": f"2026-09-{index + 1:02d}T00:00:00Z"} for index in range(20))
    todos.append({"todo_id": "todo_superseded01", "role": "agent", "status": "superseded", "text": "gone"})
    goal = {"id": GOAL, "coordination": {"agent_model": "role_v1", "registered_agents": [DEV],
                                         "agent_roles": {DEV: "developer", "ghost": "not-a-role"}}}
    board = build_goal_role_board(goal=goal, todos=todos, runtime_root=tmp_path, dispatcher={"available": False})
    assert board is not None
    assert [agent["agent_id"] for agent in board["agents"]] == [DEV]
    statuses = [card["status"] for card in board["todos"]]
    assert "superseded" not in statuses
    review = board["todos"][0]
    assert (review["todo_id"], review["status"], review["reject_count"], review["acceptor_agent"]) == (
        "todo_review000001", "in_review", 1, ACC)
    assert statuses.count("open") + statuses.count("in_review") == MAX_ROLE_BOARD_OPEN_TODOS
    assert statuses.count("done") == 12
    assert board["omitted"] == {"agents": 0, "open_todos": 6, "done_todos": 8, "gates": 0}
    assert max(len(card["text"]) for card in board["todos"]) <= 160
    # Newest completed work is kept.
    assert board["todos"][-12]["updated_at"] == "2026-09-20T00:00:00Z"
    assert len(json.dumps(board)) < 40_000
