from __future__ import annotations

import contextlib
import io
import json
import plistlib
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from loopx.cli import main as cli_main
from loopx.control_plane.turn_driver.codex_cli import RESULT_PATH_HYGIENE_INSTRUCTION, _prompt
from loopx.dispatch import DispatchConfig, Dispatcher, DispatchLock, DispatchLockError, dispatch_status
from loopx.dispatch import policy
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
from tests.test_loopx_turn_codex_cli import _request

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class ScriptedShouldRun:
    """Stands in for quota should-run: per agent, a queue of selected todos."""

    def __init__(self, todos: dict[str, list[str | None]] | None = None) -> None:
        self.todos = {agent: list(queue) for agent, queue in (todos or {}).items()}
        self.calls: list[str] = []

    def __call__(self, goal_id: str, agent_id: str) -> dict[str, Any]:
        self.calls.append(agent_id)
        queue = self.todos.get(agent_id)
        if queue is None:
            return {"should_run": True, "effective_action": "normal_run"}
        if not queue:
            return {"should_run": False, "reason": "no eligible work"}
        todo_id = queue[0] if len(queue) == 1 else queue.pop(0)
        if todo_id is None:
            return {"should_run": True, "effective_action": "normal_run"}
        return {
            "should_run": True,
            "effective_action": "normal_run",
            "selected_todo": {"todo_id": todo_id, "role": "agent"},
        }


def _dispatcher(fixture: dict[str, Any], *, should_run=None, clock=None, **overrides) -> Dispatcher:
    config = DispatchConfig(
        registry_path=fixture["registry"],
        runtime_root=fixture["runtime"],
        goal_ids=[GOAL_ID],
        no_global_sync=True,
        environ=fixture["environ"],
        loopx_argv=(sys.executable, str(fixture["fake_loopx"])),
        **overrides,
    )
    return Dispatcher(config, should_run=should_run, clock=clock or Clock())


def _release_and_wait(fixture: dict[str, Any], dispatcher: Dispatcher) -> list[dict[str, Any]]:
    fixture["release"].write_text("go", encoding="utf-8")
    return dispatcher.wait_for_children(list(dispatcher.children), timeout=30)


def _user_gates(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    listed = list_goal_todos(
        registry_path=fixture["registry"],
        goal_id=GOAL_ID,
        role="user",
        status="open",
        runtime_root_arg=str(fixture["runtime"]),
    )
    return listed["todos"]


# --- policy -----------------------------------------------------------------


def test_policy_slots_backoff_and_decisions() -> None:
    assert policy.slot_limit("orchestrator", 5) == 1
    assert policy.slot_limit("developer", 3) == 3
    assert policy.slot_limit("acceptor", 0) == 1
    assert [policy.backoff_seconds(n, base=60, cap=600) for n in (1, 2, 3, 4, 5)] == [
        60,
        120,
        240,
        480,
        600,
    ]
    selected = {"should_run": True, "selected_todo": {"todo_id": "todo_aaa", "role": "agent"}}
    assert policy.decide_turn(selected, role="developer", state_changed=False)["todo_id"] == "todo_aaa"
    idle = {"should_run": True, "effective_action": "normal_run"}
    assert policy.decide_turn(idle, role="developer", state_changed=True)["launch"] is False
    assert policy.decide_turn(idle, role="orchestrator", state_changed=False)["launch"] is False
    # run-once refuses host routes without todo lineage, so a todo-less
    # orchestrator Turn is never launched (E2E pilot: a relaunch hot loop).
    assert policy.decide_turn(idle, role="orchestrator", state_changed=True)["launch"] is False
    replan = {"should_run": True, "effective_action": "autonomous_replan"}
    pending = policy.decide_turn(replan, role="orchestrator", state_changed=False)
    assert pending == {"launch": False, "reason": "orchestrator_action_without_todo",
                       "detail": "effective_action=autonomous_replan"}
    orch_todo = {"should_run": True, "selected_todo": {"todo_id": "todo_orch", "role": "agent"}}
    assert policy.decide_turn(orch_todo, role="orchestrator", state_changed=False)["todo_id"] == "todo_orch"
    assert policy.decide_turn({"should_run": False}, role="orchestrator", state_changed=True)["launch"] is False
    crashed = policy.classify_outcome(9, "Traceback")
    assert crashed["outcome"] == "crashed"
    limited = policy.classify_outcome(1, json.dumps({"ok": False, "host_failure": {"kind": "rate_limited"}}))
    assert (limited["outcome"], limited["failure_kind"]) == ("host_failed", "rate_limited")
    assert policy.classify_outcome(0, json.dumps({"ok": True}))["outcome"] == "committed"


# --- slots ------------------------------------------------------------------


def test_orchestrator_is_serial_and_developer_fills_its_slots(tmp_path: Path) -> None:
    fixture = write_fixture(
        tmp_path,
        agents={
            "orch": {"role": "orchestrator", "max_concurrency": 3},
            "dev": {"role": "developer", "max_concurrency": 2},
            "acc": {"role": "acceptor", "runtime": "codex-cli"},
        },
    )
    set_modes(fixture, {"orch": ["hold"], "dev": ["hold"], "acc": ["hold"]})
    should_run = ScriptedShouldRun({"orch": ["todo_orch"], "dev": ["todo_aaa", "todo_bbb", "todo_ccc"], "acc": []})
    dispatcher = _dispatcher(fixture, should_run=should_run)

    report = dispatcher.reconcile()
    launched = [(item["agent_id"], item["todo_id"]) for item in report["launched"]]
    assert launched == [("orch", "todo_orch"), ("dev", "todo_aaa"), ("dev", "todo_bbb")]
    assert {"agent_id": "acc", "goal_id": GOAL_ID, "reason": "should_run_false", "detail": "no eligible work"} in report["skipped"]

    second = dispatcher.reconcile()
    assert second["launched"] == []
    reasons = {(item["agent_id"], item["reason"]) for item in second["skipped"]}
    assert ("orch", "slots_full") in reasons
    assert ("dev", "slots_full") in reasons

    status = dispatch_status(fixture["runtime"])
    assert status["agent_slots"]["orch"]["max"] == 1
    assert status["agent_slots"]["dev"] == {
        "role": "developer",
        "max": 2,
        "provider": "anthropic-login",
        "running": 2,
    }
    assert len(status["running"]) == 3

    finished = _release_and_wait(fixture, dispatcher)
    assert sorted(item["outcome"] for item in finished) == ["committed"] * 3
    assert dispatch_status(fixture["runtime"])["running"] == []


def test_global_cap_limits_total_running_turns(tmp_path: Path) -> None:
    fixture = write_fixture(
        tmp_path,
        agents={
            "dev1": {"role": "developer", "max_concurrency": 3},
            "dev2": {"role": "developer", "max_concurrency": 3},
        },
    )
    set_modes(fixture, {"dev1": ["hold"], "dev2": ["hold"]})
    should_run = ScriptedShouldRun({"dev1": ["todo_aaa", "todo_bbb", "todo_ccc"], "dev2": ["todo_ddd", "todo_eee"]})
    dispatcher = _dispatcher(fixture, should_run=should_run, max_global=2)
    report = dispatcher.reconcile()
    assert [item["todo_id"] for item in report["launched"]] == ["todo_aaa", "todo_bbb"]
    assert any(item["reason"] == "global_cap" for item in report["skipped"])
    _release_and_wait(fixture, dispatcher)


def test_same_todo_is_not_launched_twice_and_disabled_agents_are_skipped(tmp_path: Path) -> None:
    fixture = write_fixture(
        tmp_path,
        agents={
            "dev": {"role": "developer", "max_concurrency": 3},
            "off": {"role": "developer", "enabled": False},
        },
    )
    set_modes(fixture, {"dev": ["hold"]})
    dispatcher = _dispatcher(fixture, should_run=ScriptedShouldRun({"dev": ["todo_same"], "off": ["todo_xxx"]}))
    report = dispatcher.reconcile()
    assert [item["todo_id"] for item in report["launched"]] == ["todo_same"]
    reasons = {(item["agent_id"], item["reason"]) for item in report["skipped"]}
    assert ("dev", "todo_in_flight") in reasons
    assert ("off", "agent_disabled") in reasons
    _release_and_wait(fixture, dispatcher)


# --- auth preflight -----------------------------------------------------------


def test_preflight_failure_marks_agent_unavailable_and_opens_one_gate(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    fixture["environ"]["FAKE_CLAUDE_AUTH"] = "out"
    clock = Clock()
    dispatcher = _dispatcher(
        fixture, should_run=ScriptedShouldRun({"dev": ["todo_aaa"]}), clock=clock, auth_cooldown_seconds=60
    )

    first = dispatcher.reconcile()
    assert first["launched"] == []
    assert len(first["gates_opened"]) == 1
    gates = _user_gates(fixture)
    assert len(gates) == 1 and "dev needs re-login" in gates[0]["text"]
    assert gates[0]["task_class"] == "user_gate"

    # Still unavailable: skipped by the cooldown without another probe.
    assert any(item["reason"] == "agent_cooldown" for item in dispatcher.reconcile()["skipped"])
    # The cooldown expires, the preflight fails again, but the open gate is reused.
    clock.now += 120
    third = dispatcher.reconcile()
    assert third["launched"] == [] and third["gates_opened"] == []
    assert len(_user_gates(fixture)) == 1
    # A fresh dispatcher (state lost) adopts the open gate instead of duplicating it.
    (fixture["runtime"] / "dispatch" / "state.json").unlink()
    clock.now += 120
    fresh = _dispatcher(fixture, should_run=ScriptedShouldRun({"dev": ["todo_aaa"]}), clock=clock)
    assert fresh.reconcile()["gates_opened"] == []
    assert len(_user_gates(fixture)) == 1
    assert read_jsonl(fixture["turn_log"]) == []

    # After re-login the agent launches again.
    fixture["environ"]["FAKE_CLAUDE_AUTH"] = "in"
    clock.now += 600
    fresh = _dispatcher(fixture, should_run=ScriptedShouldRun({"dev": ["todo_aaa"]}), clock=clock)
    assert [item["agent_id"] for item in fresh.run_once()["launched"]] == ["dev"]


# --- provider cooldown --------------------------------------------------------


def test_rate_limit_puts_only_that_provider_into_backoff_and_long_cooldown_opens_one_gate(tmp_path: Path) -> None:
    fixture = write_fixture(
        tmp_path,
        agents={
            "claude-dev": {"role": "developer"},
            "codex-dev": {"role": "developer", "runtime": "codex-cli"},
        },
    )
    set_modes(fixture, {"claude-dev": ["rate_limited"], "codex-dev": ["ok"]})
    clock = Clock()
    should_run = ScriptedShouldRun({"claude-dev": ["todo_aaa"], "codex-dev": ["todo_bbb"]})
    dispatcher = _dispatcher(
        fixture,
        should_run=should_run,
        clock=clock,
        backoff_base_seconds=10,
        long_cooldown_seconds=40,
    )

    first = dispatcher.run_once()
    outcomes = {item["agent_id"]: (item["outcome"], item["failure_kind"]) for item in first["finished"]}
    assert outcomes == {"claude-dev": ("host_failed", "rate_limited"), "codex-dev": ("committed", None)}
    cooldown = load_state(fixture["runtime"])["provider_cooldowns"]["anthropic-login"]
    assert (cooldown["failures"], cooldown["seconds"]) == (1, 10)
    assert "codex-login" not in load_state(fixture["runtime"])["provider_cooldowns"]

    clock.now += 5
    second = dispatcher.run_once()
    assert [item["agent_id"] for item in second["launched"]] == ["codex-dev"]
    assert any(
        item["agent_id"] == "claude-dev" and item["reason"] == "provider_cooldown" for item in second["skipped"]
    )

    seconds = []
    for _ in range(3):
        clock.now = load_state(fixture["runtime"])["provider_cooldowns"]["anthropic-login"]["until"] + 1
        dispatcher.run_once()
        seconds.append(load_state(fixture["runtime"])["provider_cooldowns"]["anthropic-login"]["seconds"])
    assert seconds == [20, 40, 80]
    gates = _user_gates(fixture)
    assert len(gates) == 1
    assert "anthropic-login is in a long cooldown" in gates[0]["text"]


def test_success_resets_provider_backoff(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    set_modes(fixture, {"dev": ["quota", "ok"]})
    clock = Clock()
    dispatcher = _dispatcher(fixture, should_run=ScriptedShouldRun({"dev": ["todo_aaa"]}), clock=clock)
    dispatcher.run_once()
    assert load_state(fixture["runtime"])["provider_cooldowns"]["anthropic-login"]["kind"] == "quota_exhausted"
    clock.now += 3600
    dispatcher.run_once()
    assert load_state(fixture["runtime"])["provider_cooldowns"]["anthropic-login"]["failures"] == 0


# --- once, lock, crash --------------------------------------------------------


def test_once_is_idempotent_and_adopts_running_turns(tmp_path: Path) -> None:
    fixture = write_fixture(
        tmp_path, agents={"orch": {"role": "orchestrator"}, "dev": {"role": "developer"}}
    )
    set_modes(fixture, {"orch": ["ok"], "dev": ["hold"]})
    should_run = ScriptedShouldRun({"orch": ["todo_orch", None], "dev": ["todo_aaa"]})
    first = _dispatcher(fixture, should_run=should_run)
    report = first.run_once(wait=False)
    assert {item["agent_id"] for item in report["launched"]} == {"orch", "dev"}
    first.wait_for_children([r["run_id"] for r in report["launched"] if r["agent_id"] == "orch"], timeout=30)

    # A second process sees the running developer Turn and launches nothing:
    # the todo is in flight and the orchestrator has no todo of its own.
    second = _dispatcher(fixture, should_run=should_run)
    again = second.run_once(wait=False)
    assert again["launched"] == []
    reasons = {(item["agent_id"], item["reason"]) for item in again["skipped"]}
    assert ("orch", "orchestrator_idle") in reasons
    assert ("dev", "slots_full") in reasons

    fixture["release"].write_text("go", encoding="utf-8")
    for child in first.children.values():
        child.wait(timeout=30)
    third = _dispatcher(fixture, should_run=ScriptedShouldRun({"dev": []}))
    reaped = third.run_once(wait=False)["reaped"]
    assert [(item["agent_id"], item["outcome"]) for item in reaped] == [("dev", "committed")]
    assert len(read_jsonl(fixture["turn_log"])) == 2


def test_lock_prevents_a_second_dispatcher(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    with DispatchLock(fixture["runtime"]):
        with pytest.raises(DispatchLockError):
            _dispatcher(fixture, should_run=ScriptedShouldRun()).run_once()
        completed = subprocess.run(
            [
                sys.executable, "-m", "loopx.cli",
                "--registry", str(fixture["registry"]),
                "--runtime-root", str(fixture["runtime"]),
                "--format", "json",
                "dispatch", "serve", "--goal-id", GOAL_ID, "--once",
            ],
            cwd=ROOT,
            env={**fixture["environ"], "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.returncode == 3, completed.stderr
        assert json.loads(completed.stdout)["error"] == "dispatcher_locked"
        assert dispatch_status(fixture["runtime"])["serving"] is True
    assert dispatch_status(fixture["runtime"])["serving"] is False


def test_child_crash_is_relaunched_with_the_same_turn_identity(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    set_modes(fixture, {"dev": ["crash", "ok"]})
    dispatcher = _dispatcher(fixture, should_run=ScriptedShouldRun({"dev": ["todo_aaa"]}))
    first = dispatcher.run_once()
    assert first["finished"][0]["outcome"] == "crashed"
    crashed_id = first["launched"][0]["turn_instance_id"]
    assert load_state(fixture["runtime"])["retry_turns"][f"{GOAL_ID}/dev"]["turn_instance_id"] == crashed_id

    second = dispatcher.run_once()
    assert second["launched"][0]["turn_instance_id"] == crashed_id
    assert second["launched"][0]["turn_instance_reused"] is True
    assert second["finished"][0]["outcome"] == "committed"
    assert load_state(fixture["runtime"])["retry_turns"] == {}
    argvs = [row["argv"] for row in read_jsonl(fixture["turn_log"])]
    assert [argv[argv.index("--turn-instance-id") + 1] for argv in argvs] == [crashed_id, crashed_id]


def test_repeated_failures_on_one_todo_back_off(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    set_modes(fixture, {"dev": ["crash"]})
    clock = Clock()
    dispatcher = _dispatcher(fixture, should_run=ScriptedShouldRun({"dev": ["todo_aaa"]}), clock=clock)
    for _ in range(3):
        dispatcher.run_once()
    assert load_state(fixture["runtime"])["todo_cooldowns"][f"{GOAL_ID}/todo_aaa"]["reason"] == "repeated_crash"
    report = dispatcher.run_once()
    assert report["launched"] == []
    assert any(item["reason"] == "todo_cooldown" for item in report["skipped"])


# --- launch arguments and prompt hygiene ---------------------------------------


def test_launch_passes_agent_host_args_and_a_repo_relative_path_prompt(tmp_path: Path) -> None:
    fixture = write_fixture(
        tmp_path,
        agents={
            "dev": {"role": "developer", "system_prompt": "Project conventions.\n"},
            "acc": {"role": "acceptor", "runtime": "codex-cli"},
        },
    )
    dispatcher = _dispatcher(
        fixture,
        should_run=ScriptedShouldRun({"dev": ["todo_aaa"], "acc": ["todo_bbb"]}),
        default_validation_argv=("true",),
    )
    dispatcher.run_once()
    rows = {row["agent"]: row for row in read_jsonl(fixture["turn_log"])}
    dev = rows["dev"]["argv"]
    assert dev[dev.index("turn") : dev.index("turn") + 2] == ["turn", "run-once"]
    assert dev[dev.index("--host") + 1] == "claude-code"
    assert dev[dev.index("--claude-permission-mode") + 1] == "acceptEdits"
    assert dev[dev.index("--claude-provider") + 1] == "anthropic-login"
    assert dev[dev.index("--registry") + 1] == str(fixture["registry"])
    assert dev[dev.index("--runtime-root") + 1] == str(fixture["runtime"])
    assert json.loads(dev[dev.index("--validation-command-json") + 1]) == ["true"]
    assert "--execute" in dev and "--no-global-sync" in dev
    # Without task repositories there is no workspace and no --todo-id pin.
    assert "--todo-id" not in dev
    prompt = rows["dev"]["system_prompt"]
    assert prompt.startswith("Project conventions.")
    assert RESULT_PATH_HYGIENE_INSTRUCTION in prompt
    acc = rows["acc"]["argv"]
    assert acc[acc.index("--host") + 1] == "codex-cli"
    assert "--claude-system-prompt-file" not in acc
    # Composed prompt files are removed once the Turn is reaped.
    assert list((fixture["runtime"] / "dispatch" / "runs").glob("*.system-prompt.md")) == []


def test_turn_prompt_tells_every_host_to_use_repo_relative_paths() -> None:
    assert RESULT_PATH_HYGIENE_INSTRUCTION in _prompt(_request())
    assert "absolute local paths" in RESULT_PATH_HYGIENE_INSTRUCTION


# --- real turn run-once end to end -----------------------------------------------


def test_end_to_end_real_run_once_in_a_prepared_workspace_replays_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    api = make_repo(tmp_path, "api")
    fixture = write_fixture(
        tmp_path,
        agents={"dev": {"role": "developer", "system_prompt": "Be precise.\n"}},
        repos={"api": api},
    )
    added = add_goal_todo(
        registry_path=fixture["registry"],
        goal_id=GOAL_ID,
        runtime_root_arg=str(fixture["runtime"]),
        role="agent",
        text="Build the api fixture",
        priority="P0",
        task_class="advancement_task",
        action_kind="fixture",
        role_contract={"task_repositories": ["api"]},
    )
    todo_id = added["todo_id"]
    validator = (sys.executable, "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('fixture-artifact.txt').exists() else 3)")
    config = DispatchConfig(
        registry_path=fixture["registry"],
        runtime_root=fixture["runtime"],
        goal_ids=[GOAL_ID],
        no_global_sync=True,
        environ={**fixture["environ"], "PYTHONPATH": str(ROOT)},
        turn_timeout_seconds=120,
        default_validation_argv=validator,
    )
    dispatcher = Dispatcher(config)  # real quota should-run, preflight and turn run-once

    report = dispatcher.run_once()
    assert [(item["agent_id"], item["todo_id"]) for item in report["launched"]] == [("dev", todo_id)]
    assert report["finished"][0]["outcome"] == "committed", report
    workspace = fixture["runtime"] / "goals" / GOAL_ID / "workspaces" / todo_id / "api"
    assert git(workspace, "rev-parse", "--abbrev-ref", "HEAD") == f"loopx/{GOAL_ID}/{todo_id}"
    calls = read_jsonl(fixture["claude_log"])
    assert len(calls) == 1
    assert Path(calls[0]["cwd"]).resolve() == workspace.resolve()
    assert calls[0]["system_prompt"].startswith("Be precise.")
    assert "repo `api`" in calls[0]["system_prompt"]
    assert RESULT_PATH_HYGIENE_INSTRUCTION in calls[0]["prompt"]

    # Pretend the child crashed after settling: the relaunch reuses the Turn
    # identity and run-once replays instead of invoking the host or spending again.
    committed = load_state(fixture["runtime"])["history"][-1]
    committed_key = json.loads(Path(committed["stdout_path"]).read_text())["resume_turn_key"]
    dispatcher.state["retry_turns"][f"{GOAL_ID}/dev"] = {
        "turn_instance_id": committed["turn_instance_id"],
        "todo_id": todo_id,
    }
    replay = dispatcher.run_once()
    assert replay["launched"][0]["turn_instance_reused"] is True
    payload = json.loads(Path(load_state(fixture["runtime"])["history"][-1]["stdout_path"]).read_text())
    assert replay["finished"][0]["outcome"] == "committed", json.dumps(payload)[:4000]
    assert payload["replayed"] is True
    assert load_state(fixture["runtime"])["history"][-1]["resume_turn_key"] == committed_key
    assert len(read_jsonl(fixture["claude_log"])) == 1


# --- serve loop and CLI -----------------------------------------------------------


def test_serve_reconciles_on_state_file_events(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    should_run = ScriptedShouldRun({"dev": []})
    dispatcher = _dispatcher(
        fixture, should_run=should_run, tick_seconds=3600, poll_seconds=0.05, event_debounce_seconds=0
    )
    triggers: list[str] = []
    done = threading.Event()

    def on_pass(report: dict[str, Any]) -> None:
        triggers.append(report["trigger"])
        if report["trigger"] == "state_event":
            done.set()

    thread = threading.Thread(target=dispatcher.serve, kwargs={"stop": done.is_set, "on_pass": on_pass})
    thread.start()
    try:
        deadline = time.time() + 10
        while not triggers and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)
        fixture["state"].write_text(fixture["state"].read_text() + "\n<!-- touched -->\n", encoding="utf-8")
        assert done.wait(10), triggers
    finally:
        done.set()
        thread.join(10)
    assert triggers[0] == "tick" and "state_event" in triggers


def _cli(argv: list[str]) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli_main(argv)
    return code, output.getvalue()


def test_status_and_launchd_plist_cli(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    base = ["--registry", str(fixture["registry"]), "--runtime-root", str(fixture["runtime"])]
    code, text = _cli([*base, "--format", "json", "dispatch", "status"])
    assert code == 0
    status = json.loads(text)
    assert status["serving"] is False and status["running"] == []

    code, text = _cli(
        [*base, "dispatch", "launchd-plist", "--goal-id", GOAL_ID, "--tick-seconds", "30", "--max-global", "3"]
    )
    assert code == 0
    plist = plistlib.loads(text.encode("utf-8"))
    args = plist["ProgramArguments"]
    assert args[1:3] == ["-m", "loopx.cli"]
    assert args[args.index("serve") - 1] == "dispatch"
    assert args[args.index("--goal-id") + 1] == GOAL_ID
    assert args[args.index("--max-global") + 1] == "3"
    assert args[args.index("--runtime-root") + 1] == str(fixture["runtime"])
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["Label"].startswith("com.loopx.dispatch.")
    # Rendering never installs anything.
    assert not (Path.home() / "Library" / "LaunchAgents" / f"{plist['Label']}.plist").exists()


def test_orchestrator_plan_todo_is_validated_by_an_applied_plan_card(tmp_path: Path) -> None:
    """E2E pilot: the intake planning todo had no validator, so every orchestrator Turn failed."""

    fixture = write_fixture(
        tmp_path,
        agents={"orch": {"role": "orchestrator"}, "dev": {"role": "developer"}},
    )

    def add(text: str, action_kind: str, **extra: Any) -> str:
        added = add_goal_todo(
            registry_path=fixture["registry"], goal_id=GOAL_ID, runtime_root_arg=str(fixture["runtime"]),
            role="agent", text=text, task_class="advancement_task", action_kind=action_kind, **extra,
        )
        return str(added["todo_id"])

    plan_todo = add("Clarify and plan", "plan", claimed_by="orch",
                    role_contract={"required_role": "orchestrator", "requires_acceptance": False})
    build_todo = add("Build it", "fixture")
    dispatcher = _dispatcher(
        fixture, should_run=ScriptedShouldRun({"orch": [plan_todo], "dev": [build_todo]})
    )
    dispatcher.run_once()
    rows = {row["agent"]: row for row in read_jsonl(fixture["turn_log"])}
    orch = rows["orch"]["argv"]
    validator = json.loads(orch[orch.index("--validation-command-json") + 1])
    assert validator[-6:] == ["plan", "list", "--goal-id", GOAL_ID, "--require-status", "applied"]
    assert validator[validator.index("--registry") + 1] == str(fixture["registry"])
    # Ordinary developer work without a declared validator gets no plan check.
    assert "--validation-command-json" not in rows["dev"]["argv"]
    prompt = rows["orch"]["system_prompt"]
    assert "user_action_required" in prompt and "--registry" in prompt

    def plan_list() -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return cli_main([
                "--registry", str(fixture["registry"]), "--runtime-root", str(fixture["runtime"]),
                "--format", "json", "plan", "list", "--goal-id", GOAL_ID, "--require-status", "applied",
            ])

    assert plan_list() == 1
    plans = fixture["runtime"] / "goals" / GOAL_ID / "plans"
    plans.mkdir(parents=True)
    (plans / "plan_aaaa.json").write_text(json.dumps({"plan_id": "plan_aaaa", "status": "applied"}))
    assert plan_list() == 0


def test_a_second_slot_takes_the_lanes_next_executable_todo(tmp_path: Path) -> None:
    """E2E pilot: should-run always selects the first todo, so max_concurrency=2 ran one Turn."""

    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer", "max_concurrency": 2}})
    set_modes(fixture, {"dev": ["hold"]})

    def should_run(goal_id: str, agent_id: str) -> dict[str, Any]:
        items = [{"todo_id": "todo_aaa", "status": "open"}, {"todo_id": "todo_bbb", "status": "open"},
                 {"todo_id": "todo_ccc", "status": "done"}]
        return {"should_run": True, "effective_action": "normal_run",
                "selected_todo": {"todo_id": "todo_aaa", "role": "agent"},
                "agent_todo_summary": {"first_executable_items": items}}

    dispatcher = _dispatcher(fixture, should_run=should_run)
    report = dispatcher.reconcile()
    assert [(item["agent_id"], item["todo_id"], item["reason"]) for item in report["launched"]] == [
        ("dev", "todo_aaa", "selected_todo"), ("dev", "todo_bbb", "alternate_todo")]
    _release_and_wait(fixture, dispatcher)
    rows = [row["argv"] for row in read_jsonl(fixture["turn_log"])]
    pinned = [argv[argv.index("--todo-id") + 1] for argv in rows if "--todo-id" in argv]
    assert pinned == ["todo_bbb"]
    assert policy.alternate_todo({"agent_todo_summary": {"first_executable_items": [
        {"todo_id": "todo_aaa"}, {"todo_id": "todo_ccc", "status": "done"}]}}, exclude={"todo_aaa"}) is None


def test_orchestrator_escalation_todo_is_validated_by_the_escalated_todo_status(tmp_path: Path) -> None:
    """E2E pilot: an escalation todo had no validator, so the orchestrator could not settle it."""

    from loopx.dispatch.checks import main as check

    fixture = write_fixture(tmp_path, agents={"orch": {"role": "orchestrator"}, "dev": {"role": "developer"}})
    rejected = add_goal_todo(
        registry_path=fixture["registry"], goal_id=GOAL_ID, runtime_root_arg=str(fixture["runtime"]),
        role="agent", text="Build the web UI", task_class="advancement_task", action_kind="fixture",
    )["todo_id"]
    escalation = add_goal_todo(
        registry_path=fixture["registry"], goal_id=GOAL_ID, runtime_root_arg=str(fixture["runtime"]),
        role="agent", text=f"Escalation: {rejected} was rejected 2 times by acc. Decide: reassign.",
        task_class="advancement_task", action_kind="replan", claimed_by="orch",
        role_contract={"required_role": "orchestrator", "requires_acceptance": False},
    )["todo_id"]
    dispatcher = _dispatcher(fixture, should_run=ScriptedShouldRun({"orch": [escalation], "dev": []}))
    dispatcher.run_once()
    argv = {row["agent"]: row["argv"] for row in read_jsonl(fixture["turn_log"])}["orch"]
    validator = json.loads(argv[argv.index("--validation-command-json") + 1])
    assert validator[1:4] == ["-m", "loopx.dispatch.checks", "todo-not-status"]
    assert validator[validator.index("--todo-id") + 1] == rejected
    base = ["todo-not-status", "--registry", str(fixture["registry"]), "--runtime-root", str(fixture["runtime"]),
            "--goal-id", GOAL_ID, "--todo-id", rejected]
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert check([*base, "--status", "blocked"]) == 0  # the rejected todo is open
        assert check([*base, "--status", "open"]) == 1
        assert check([*base[:-1], "todo_000000000000", "--status", "blocked"]) == 1
