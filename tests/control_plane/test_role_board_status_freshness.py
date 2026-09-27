"""Pilot v1 gap N3: the status todo index and role board follow the goal state.

CLI lifecycle writes such as ``gate resolve`` append no ``todo_*`` rollout
event, and ``todo supersede --by`` appends one whose status is only the command
name. The status todo index must still show what the goal state file says, and
the role board must leave superseded todos out.
"""
from __future__ import annotations

import json
from pathlib import Path

from test_gates_plans_intake import DEV, GOAL, ORCH, fixture

from loopx.cli import main
from loopx.control_plane.todos.todo_index import build_todo_index
from loopx.rollout_event_log import (
    append_rollout_event,
    build_rollout_event,
    load_rollout_events,
    rollout_event_log_path,
)
from loopx.status import collect_status, public_safe_compact_text


def _cli(registry: Path, runtime: Path, *args: str) -> tuple[int, dict]:
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["--registry", str(registry), "--runtime-root", str(runtime), "--format", "json", *args])
    return code, json.loads(out.getvalue())


def _goal_payload(registry: Path, runtime: Path) -> tuple[dict, dict]:
    payload = collect_status(registry_path=registry, runtime_root_override=str(runtime), scan_roots=[], limit=5)
    index = {item["todo_id"]: item for item in payload["todo_index"]["items"] if item["goal_id"] == GOAL}
    goal = next(goal for goal in payload["run_history"]["goals"] if goal["id"] == GOAL)
    return index, goal["role_board"]


def test_cli_gate_resolve_and_supersede_by_keep_status_and_board_current(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    # The orchestrator opens a gate through the CLI: a todo_add event with status open.
    code, gate = _cli(registry, runtime, "todo", "add", "--goal-id", GOAL, "--role", "user",
                      "--task-class", "user_gate", "--agent-id", ORCH, "--text", "Confirm closure")
    assert code == 0
    gate_id = gate["todo_id"]
    code, old = _cli(registry, runtime, "todo", "add", "--goal-id", GOAL, "--role", "agent",
                     "--claimed-by", DEV, "--text", "Old approach")
    assert code == 0
    code, new = _cli(registry, runtime, "todo", "add", "--goal-id", GOAL, "--role", "agent",
                     "--claimed-by", DEV, "--text", "New approach")
    assert code == 0
    code, throwaway = _cli(registry, runtime, "todo", "add", "--goal-id", GOAL, "--role", "agent",
                           "--claimed-by", DEV, "--text", "Throwaway check")
    assert code == 0

    # The owner approves the gate (no todo_* event) and the orchestrator supersedes.
    assert _cli(registry, runtime, "gate", "resolve", "--goal-id", GOAL, "--todo-id", gate_id,
                "--decision", "approve")[0] == 0
    assert _cli(registry, runtime, "todo", "supersede", "--goal-id", GOAL, "--todo-id", old["todo_id"],
                "--by", new["todo_id"], "--agent-id", ORCH)[0] == 0
    assert _cli(registry, runtime, "todo", "supersede", "--goal-id", GOAL, "--todo-id", throwaway["todo_id"],
                "--agent-id", DEV)[0] == 0
    events = load_rollout_events(rollout_event_log_path(runtime, GOAL))
    assert not [e for e in events if e.get("todo_id") == gate_id and e["event_kind"] != "todo_add"]
    assert any(e["event_kind"] == "todo_supersede" and e.get("todo_id") == old["todo_id"] for e in events)

    index, board = _goal_payload(registry, runtime)
    assert (index[gate_id]["status"], index[gate_id]["done"]) == ("done", True)
    assert (index[old["todo_id"]]["status"], index[old["todo_id"]]["done"]) == ("done", True)
    assert index[throwaway["todo_id"]]["status"] == "done"
    # Rollout metadata is still folded in.
    assert index[old["todo_id"]]["latest_event_kind"] == "todo_supersede"

    cards = {card["todo_id"] for card in board["todos"]}
    assert new["todo_id"] in cards
    assert old["todo_id"] not in cards and throwaway["todo_id"] not in cards
    assert gate_id not in {row["todo_id"] for row in board["gates"]}


def test_event_only_supersede_projects_done_not_deferred(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    event = build_rollout_event(goal_id=GOAL, event_kind="todo_supersede", todo_id="todo_aaaaaaaaaaaa",
                                agent_id=ORCH, status="supersede", recorded_at="2026-09-27T08:47:26Z")
    append_rollout_event(rollout_event_log_path(runtime, GOAL), event)
    index = build_todo_index(queue={"items": []}, history={"goals": [{"id": GOAL}]}, runtime_root=runtime,
                             public_safe_compact_text=public_safe_compact_text)
    item = next(item for item in index["items"] if item["todo_id"] == "todo_aaaaaaaaaaaa")
    assert (item["status"], item["done"]) == ("done", True)


def test_older_event_status_never_overrides_the_attention_queue(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    for kind, status in (("todo_add", "open"), ("todo_review_blocked", "review_blocked")):
        append_rollout_event(
            rollout_event_log_path(runtime, GOAL),
            build_rollout_event(goal_id=GOAL, event_kind=kind, todo_id="todo_bbbbbbbbbbbb", agent_id=ORCH,
                                status=status, recorded_at="2026-09-27T08:00:00Z"),
        )
    queue = {"items": [{"goal_id": GOAL, "user_todos": {"items": [
        {"todo_id": "todo_bbbbbbbbbbbb", "status": "done", "done": True, "text": "Push request",
         "task_class": "user_gate"}]}}]}
    index = build_todo_index(queue=queue, history={"goals": [{"id": GOAL}]}, runtime_root=runtime,
                             public_safe_compact_text=public_safe_compact_text)
    item = index["items"][0]
    assert (item["status"], item["done"], item["event_count"]) == ("done", True, 2)
    assert item["latest_event_kind"] == "todo_review_blocked"
