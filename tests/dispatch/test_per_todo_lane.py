"""Per-todo Turn lanes under role_v1 (design-v0 decision 32, E2E pilot gap G5).

A developer with ``max_concurrency`` 2 runs two todos of one goal at once; the
same todo never runs twice; the orchestrator stays serial per goal; peer_v1
goals keep one lane per agent and goal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from loopx.control_plane.turn_driver.executor import run_loopx_turn_once
from loopx.control_plane.turn_driver.lane_fence import (
    TURN_LANE_IN_FLIGHT,
    turn_lane_singleflight,
    turn_lane_target,
    turn_lane_todo_scope,
)
from loopx.dispatch import DispatchConfig, Dispatcher, policy
from loopx.dispatch.state import load_state
from loopx.todos import add_goal_todo, list_goal_todos
from tests.dispatch.dispatch_fixtures import (
    GOAL_ID,
    git,
    git_env,
    make_repo,
    read_jsonl,
    set_modes,
    write_fixture,
)
from tests.dispatch.test_loopx_dispatcher import ScriptedShouldRun, _dispatcher, _release_and_wait

ROOT = Path(__file__).resolve().parents[2]


# --- the fence ------------------------------------------------------------------


def _plan(agent_id: str, todo_id: str | None = None) -> dict[str, Any]:
    action = {"selected_todo": {"todo_id": todo_id}} if todo_id else {}
    return {
        "host": {"kind": "dsh", "execution_mode": "isolated-headless"},
        "route": {"kind": "ready_for_host", "would_invoke_host": True},
        "turn_envelope": {"agent_id": agent_id, "goal_id": GOAL_ID, "action": action},
        "transaction": {"turn_key": "sha256:" + "2" * 64},
    }


def test_lane_scope_is_per_todo_only_for_role_v1_developers_and_acceptors() -> None:
    assert turn_lane_todo_scope(role_v1=True, agent_role="developer", todo_id="todo_a") == "todo_a"
    assert turn_lane_todo_scope(role_v1=True, agent_role="acceptor", todo_id="todo_a") == "todo_a"
    # The orchestrator stays serial per goal (decision 19).
    assert turn_lane_todo_scope(role_v1=True, agent_role="orchestrator", todo_id="todo_a") is None
    # peer_v1 goals and agents without a role keep the (agent, goal) lane.
    assert turn_lane_todo_scope(role_v1=False, agent_role="developer", todo_id="todo_a") is None
    assert turn_lane_todo_scope(role_v1=True, agent_role=None, todo_id="todo_a") is None
    # A Turn without a selected todo keeps the agent lane.
    assert turn_lane_todo_scope(role_v1=True, agent_role="developer", todo_id="") is None
    assert policy.todo_scoped_lane(role_v1=True, registry_role="developer") is True
    assert policy.todo_scoped_lane(role_v1=True, registry_role="orchestrator") is False
    assert policy.todo_scoped_lane(role_v1=False, registry_role="developer") is False


def test_todo_lanes_are_keyed_by_goal_and_todo_not_by_agent(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    first = turn_lane_target(runtime_root=root, goal_id=GOAL_ID, plan=_plan("dev"), todo_id="todo_a")
    second = turn_lane_target(runtime_root=root, goal_id=GOAL_ID, plan=_plan("dev"), todo_id="todo_b")
    other_agent = turn_lane_target(runtime_root=root, goal_id=GOAL_ID, plan=_plan("acc"), todo_id="todo_a")
    agent_lane = turn_lane_target(runtime_root=root, goal_id=GOAL_ID, plan=_plan("dev"))
    other_goal = turn_lane_target(runtime_root=root, goal_id="other-goal", plan=_plan("dev"), todo_id="todo_a")
    assert first != second
    # Any one todo has one lane whichever agent runs it.
    assert first == other_agent
    assert len({first, agent_lane, other_goal}) == 3


def _execute(tmp_path: Path, plan: dict[str, Any], todo_id: str | None) -> dict[str, Any]:
    return run_loopx_turn_once(
        plan,
        host_argv=["python3", "-c", "raise SystemExit(0)"],
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        goal_id=GOAL_ID,
        timeout_seconds=1.0,
        execute=True,
        turn_lane_todo_id=todo_id,
    )


def test_one_agent_holds_two_todo_lanes_but_never_one_todo_twice(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    with turn_lane_singleflight(runtime_root=root, goal_id=GOAL_ID, plan=_plan("dev", "todo_a"), todo_id="todo_a") as held:
        assert held is not None
        # A second Turn of the same agent on another todo takes its own lane.
        with turn_lane_singleflight(
            runtime_root=root, goal_id=GOAL_ID, plan=_plan("dev", "todo_b"), todo_id="todo_b"
        ) as second:
            assert second is not None
        # The same todo is refused, for this agent and for any other.
        for agent in ("dev", "acc"):
            refused = _execute(tmp_path, _plan(agent, "todo_a"), "todo_a")
            assert refused["reason"] == TURN_LANE_IN_FLIGHT
            assert refused["effects"]["host_invoked"] is False
            assert refused["in_flight"]["agent_id"] == "dev"
            assert refused["turn_lane"] == {"scope": "todo", "todo_id": "todo_a"}
    # The agent lane (orchestrator, peer_v1) still refuses a second Turn.
    with turn_lane_singleflight(runtime_root=root, goal_id=GOAL_ID, plan=_plan("orch", "todo_x")) as held:
        assert held is not None
        refused = _execute(tmp_path, _plan("orch", "todo_y"), None)
        assert refused["reason"] == TURN_LANE_IN_FLIGHT
        assert refused["turn_lane"] == {"scope": "agent"}


# --- the dispatcher -----------------------------------------------------------------


def test_max_concurrency_one_still_serializes_a_developer(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer", "max_concurrency": 1}})
    set_modes(fixture, {"dev": ["hold"]})
    dispatcher = _dispatcher(fixture, should_run=ScriptedShouldRun({"dev": ["todo_aaa", "todo_bbb"]}))
    report = dispatcher.reconcile()
    assert [item["todo_id"] for item in report["launched"]] == ["todo_aaa"]
    assert ("dev", "slots_full") in {(item["agent_id"], item["reason"]) for item in report["skipped"]}
    _release_and_wait(fixture, dispatcher)


def test_orchestrator_stays_serial_per_goal(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"orch": {"role": "orchestrator", "max_concurrency": 4}})
    set_modes(fixture, {"orch": ["hold"]})
    dispatcher = _dispatcher(fixture, should_run=ScriptedShouldRun({"orch": ["todo_orch_one", "todo_orch_two"]}))
    report = dispatcher.reconcile()
    assert [item["todo_id"] for item in report["launched"]] == ["todo_orch_one"]
    again = dispatcher.reconcile()
    assert again["launched"] == []
    assert ("orch", "slots_full") in {(item["agent_id"], item["reason"]) for item in again["skipped"]}
    _release_and_wait(fixture, dispatcher)


def test_peer_v1_goals_keep_one_lane_per_agent_and_goal(tmp_path: Path) -> None:
    fixture = write_fixture(
        tmp_path, agents={"dev": {"role": "developer", "max_concurrency": 3}}, agent_model="peer_v1"
    )
    set_modes(fixture, {"dev": ["hold"]})
    dispatcher = _dispatcher(fixture, should_run=ScriptedShouldRun({"dev": ["todo_aaa", "todo_bbb"]}))
    report = dispatcher.reconcile()
    assert [item["todo_id"] for item in report["launched"]] == ["todo_aaa"]
    assert ("dev", "turn_lane_in_flight") in {(item["agent_id"], item["reason"]) for item in report["skipped"]}
    _release_and_wait(fixture, dispatcher)


def test_crash_identities_of_two_todos_of_one_agent_do_not_collide(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer", "max_concurrency": 2}})
    set_modes(fixture, {"dev": ["crash", "crash", "ok"]})
    dispatcher = _dispatcher(fixture, should_run=ScriptedShouldRun({"dev": ["todo_aaa", "todo_bbb"]}))
    first = dispatcher.run_once()
    assert sorted(item["outcome"] for item in first["finished"]) == ["crashed", "crashed"]
    launched = {item["todo_id"]: item["turn_instance_id"] for item in first["launched"]}
    retries = load_state(fixture["runtime"])["retry_turns"]
    assert {key: value["turn_instance_id"] for key, value in retries.items()} == {
        f"{GOAL_ID}/todo_aaa@dev": launched["todo_aaa"],
        f"{GOAL_ID}/todo_bbb@dev": launched["todo_bbb"],
    }
    dispatcher._should_run = ScriptedShouldRun({"dev": ["todo_aaa", "todo_bbb"]})
    second = dispatcher.run_once()
    relaunched = {item["todo_id"]: item for item in second["launched"]}
    assert {todo: item["turn_instance_id"] for todo, item in relaunched.items()} == launched
    assert all(item["turn_instance_reused"] for item in relaunched.values())
    assert load_state(fixture["runtime"])["retry_turns"] == {}


# --- real turn run-once, concurrently ---------------------------------------------------


def _todo_rows(fixture: dict[str, Any]) -> dict[str, dict[str, Any]]:
    listed = list_goal_todos(
        registry_path=fixture["registry"], goal_id=GOAL_ID, role="agent",
        runtime_root_arg=str(fixture["runtime"]),
    )
    return {row["todo_id"]: row for row in listed["todos"]}


def test_one_developer_runs_two_todos_of_one_goal_concurrently_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    api = make_repo(tmp_path, "api")
    web = make_repo(tmp_path, "web")
    for repo in (api, web):
        # A credential-free origin identifies each worktree as an independent
        # delivery workspace for the multi-agent guard (a developer and an
        # acceptor are registered).
        git(repo, "remote", "add", "origin", f"https://example.com/fixture/{repo.name}.git")
    fixture = write_fixture(
        tmp_path,
        agents={
            "dev": {"role": "developer", "max_concurrency": 2},
            "acc": {"role": "acceptor", "runtime": "codex-cli"},
        },
        repos={"api": api, "web": web},
    )
    todo_ids = {}
    for repo in ("api", "web"):
        added = add_goal_todo(
            registry_path=fixture["registry"], goal_id=GOAL_ID, runtime_root_arg=str(fixture["runtime"]),
            role="agent", text=f"Build the {repo} fixture", priority="P0",
            task_class="advancement_task", action_kind="fixture",
            role_contract={"task_repositories": [repo]},
        )
        todo_ids[repo] = added["todo_id"]
    barrier = tmp_path / "barrier"
    validator = (sys.executable, "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('fixture-artifact.txt').exists() else 3)")
    config = DispatchConfig(
        registry_path=fixture["registry"],
        runtime_root=fixture["runtime"],
        goal_ids=[GOAL_ID],
        no_global_sync=True,
        environ={
            **fixture["environ"],
            "PYTHONPATH": str(ROOT),
            "FAKE_CLAUDE_BARRIER_DIR": str(barrier),
            "FAKE_CLAUDE_BARRIER_COUNT": "2",
            "FAKE_CLAUDE_RESULT_KIND": "validated_completion",
            "FAKE_CLAUDE_COMMIT": "1",
        },
        turn_timeout_seconds=180,
        default_validation_argv=validator,
    )
    dispatcher = Dispatcher(config)  # real quota should-run, preflight and turn run-once

    report = dispatcher.run_once()
    launched = sorted((item["agent_id"], item["todo_id"]) for item in report["launched"])
    assert launched == sorted(("dev", todo_id) for todo_id in todo_ids.values()), report
    outcomes = {item["todo_id"]: item["outcome"] for item in report["finished"]}
    history = {item["todo_id"]: item for item in load_state(fixture["runtime"])["history"]}
    payloads = {
        todo_id: json.loads(Path(history[todo_id]["stdout_path"]).read_text(encoding="utf-8"))
        for todo_id in todo_ids.values()
    }
    assert outcomes == {todo_id: "committed" for todo_id in todo_ids.values()}, {
        todo_id: json.dumps(payload)[:3000] for todo_id, payload in payloads.items()
    }

    # A real overlap: both host processes were running at the same time.
    probes = [json.loads(path.read_text(encoding="utf-8")) for path in barrier.glob("*.end")]
    assert len(probes) == 2 and all(probe["met"] for probe in probes), probes
    assert max(probe["started"] for probe in probes) < min(probe["ended"] for probe in probes)
    assert {Path(probe["cwd"]).name for probe in probes} == {"api", "web"}

    # Each Turn settled on its own: its own journal, its own quota spend, and
    # its own todo moved to review for the acceptor.
    turn_keys = {todo_id: payload["resume_turn_key"] for todo_id, payload in payloads.items()}
    assert len(set(turn_keys.values())) == 2
    for todo_id, payload in payloads.items():
        assert payload["status"] == "committed"
        assert payload["effects"]["host_invoked"] is True
        assert payload["quota_slot_spend_count"] == 1
        journals = list((fixture["runtime"] / "goals" / GOAL_ID / "turns").glob("*.json"))
        owned = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in journals
            if json.loads(path.read_text(encoding="utf-8")).get("turn_key") == turn_keys[todo_id]
        ]
        assert len(owned) == 1
        assert owned[0]["status"] == "committed"
        assert owned[0]["writeback"]["completion"]["todo_id"] == todo_id
    rows = _todo_rows(fixture)
    for todo_id in todo_ids.values():
        assert rows[todo_id]["status"] == "in_review", rows[todo_id]
        assert rows[todo_id].get("delivered_by") == "dev"
    events = read_jsonl(fixture["runtime"] / "goals" / GOAL_ID / "rollout-event-log.jsonl")
    spends = [event for event in events if event.get("event_kind") == "quota_spend"]
    assert sorted(event.get("todo_id") for event in spends) == sorted(todo_ids.values())
    assert len({event.get("run_id") for event in spends}) == 2
    # Each todo's work landed on its own branch, in its own repo.
    for repo, todo_id in todo_ids.items():
        workspace = fixture["runtime"] / "goals" / GOAL_ID / "workspaces" / todo_id / repo
        assert git(workspace, "log", "-1", "--format=%s") == "fixture artifact"


def test_the_same_todo_is_refused_while_its_turn_runs(tmp_path: Path) -> None:
    """Two concurrent run-once calls pinned to one todo: exactly one runs the host."""

    from tests.test_loopx_turn_driver import _write_live_fixture

    project, runtime, registry = _write_live_fixture(tmp_path)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    coordination = payload["goals"][0]["coordination"]
    coordination["agent_model"] = "role_v1"
    coordination["registered_agents"] = ["codex-fixture"]
    coordination["agent_roles"] = {"codex-fixture": "developer"}
    registry.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    goal_id = payload["goals"][0]["id"]
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    host_script = f"""
import json, os, pathlib, sys, time
request = json.load(sys.stdin)
barrier = pathlib.Path({str(barrier)!r})
(barrier / f"{{os.getpid()}}.start").write_text("x")
time.sleep(3)
json.dump({{
    "schema_version": "loopx_turn_result_v0",
    "turn_key": request["turn_key"],
    "result_kind": "validated_progress",
    "completed_phases": ["host_execute", "typed_result"],
    "classification": "fixture_progress",
    "recommended_action": "Continue the fixture.",
    "next_action": "Run the next fixture check.",
    "delivery_batch_scale": "implementation",
    "delivery_outcome": "outcome_progress",
    "vision_unchanged_reason": "The fixture objective remains unchanged.",
    "summary": "One public fixture advanced."
}}, sys.stdout)
"""
    host_project = tmp_path / "host"
    host_project.mkdir()
    results: list[dict[str, Any]] = []

    def run(instance: str) -> None:
        argv = [
            sys.executable, "-m", "loopx.cli",
            "--registry", str(registry), "--runtime-root", str(runtime), "--format", "json",
            "turn", "run-once", "--host", "generic-cli", "--goal-id", goal_id,
            "--agent-id", "codex-fixture", "--project", str(host_project),
            "--host-adapter-command-json", json.dumps([sys.executable, "-c", host_script]),
            "--validation-command-json", json.dumps([sys.executable, "-c", "pass"]),
            "--todo-id", "todo_fixture0001", "--turn-instance-id", instance,
            "--scan-root", str(project), "--no-global-sync", "--execute",
        ]
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        results.append(json.loads(completed.stdout))

    # Two processes, as the dispatcher would launch them.
    threads = [threading.Thread(target=run, args=(f"same-todo-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert len(results) == 2
    assert len(list(barrier.glob("*.start"))) == 1
    refused = [item for item in results if item.get("reason") == TURN_LANE_IN_FLIGHT]
    assert len(refused) == 1, [json.dumps(item)[:2000] for item in results]
    assert refused[0]["turn_lane"] == {"scope": "todo", "todo_id": "todo_fixture0001"}
    assert refused[0]["effects"]["host_invoked"] is False
