"""A Turn finds its goal from any cwd.

`dispatch serve` handed every Turn the registry path it was given, and the
default is relative (`.loopx/registry.json`). A developer Turn runs in its todo
worktree, so it failed before calling the model with `goal_id not found in
canonical source registry`.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from loopx.dispatch import DispatchConfig, Dispatcher
from loopx.paths import DEFAULT_PROJECT_REGISTRY
from loopx.todos import add_goal_todo
from tests.dispatch.dispatch_fixtures import GOAL_ID, git_env, make_repo, read_jsonl, write_fixture
from tests.dispatch.test_loopx_dispatcher import Clock, ScriptedShouldRun, _cli


def _from_state_home(fixture: dict[str, Any], should_run: ScriptedShouldRun) -> Dispatcher:
    """A dispatcher given the default relative registry, as `serve` run in the state home is."""

    assert not DEFAULT_PROJECT_REGISTRY.is_absolute()
    config = DispatchConfig(
        registry_path=DEFAULT_PROJECT_REGISTRY,
        runtime_root=fixture["runtime"],
        goal_ids=[GOAL_ID],
        no_global_sync=True,
        environ=fixture["environ"],
        loopx_argv=(sys.executable, str(fixture["fake_loopx"])),
    )
    return Dispatcher(config, should_run=should_run, clock=Clock())


def _option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_turn_commands_carry_an_absolute_registry_when_serve_got_the_relative_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = write_fixture(tmp_path, agents={"orch": {"role": "orchestrator"}, "dev": {"role": "developer"}})
    monkeypatch.chdir(fixture["project"])
    _from_state_home(fixture, ScriptedShouldRun({"orch": ["todo_orch"], "dev": ["todo_aaa"]})).run_once()

    turns = {turn["agent"]: turn for turn in read_jsonl(fixture["turn_log"])}
    assert sorted(turns) == ["dev", "orch"]
    for turn in turns.values():
        argv = turn["argv"]
        assert Path(_option(argv, "--registry")) == fixture["registry"].resolve()
        assert Path(_option(argv, "--runtime-root")).is_absolute()
        assert Path(_option(argv, "--project")).is_absolute()
    # The CLI prefix the orchestrator is told to use names the same registry.
    assert f"--registry {fixture['registry'].resolve()}" in turns["orch"]["system_prompt"]


def test_the_launchd_plist_carries_an_absolute_registry_when_rendered_in_the_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    monkeypatch.delenv("LOOPX_REGISTRY", raising=False)
    monkeypatch.chdir(fixture["project"])
    code, text = _cli(["dispatch", "launchd-plist", "--goal-id", GOAL_ID, "--project", "."])

    assert code == 0
    plist = plistlib.loads(text.encode("utf-8"))
    args = plist["ProgramArguments"]
    # launchd starts the job in the runtime root, not in the state home.
    assert Path(plist["WorkingDirectory"]) == fixture["runtime"]
    assert Path(_option(args, "--registry")) == fixture["registry"].resolve()
    assert Path(_option(args, "--runtime-root")).is_absolute()
    assert Path(_option(args, "--project")) == fixture["project"].resolve()


def test_a_developer_turn_command_resolves_its_goal_from_the_todo_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    api = make_repo(tmp_path, "api")
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}}, repos={"api": api})
    todo_id = add_goal_todo(
        registry_path=fixture["registry"], goal_id=GOAL_ID, runtime_root_arg=str(fixture["runtime"]),
        role="agent", text="Build the api fixture", priority="P0", task_class="advancement_task",
        action_kind="fixture", role_contract={"task_repositories": ["api"]},
    )["todo_id"]
    monkeypatch.chdir(fixture["project"])
    _from_state_home(fixture, ScriptedShouldRun({"dev": [todo_id]})).run_once()

    [turn] = read_jsonl(fixture["turn_log"])
    worktree = Path(turn["cwd"])
    assert worktree.resolve() == (fixture["runtime"] / "goals" / GOAL_ID / "workspaces" / todo_id / "api").resolve()
    # Replay the Turn's command against the real CLI from the worktree, as a
    # dry run: no --execute, so no host is called.
    argv = [item for item in turn["argv"] if item != "--execute"]
    prompt = Path(_option(argv, "--claude-system-prompt-file"))
    prompt.write_text(turn["system_prompt"], encoding="utf-8")  # removed when the Turn was reaped
    result = subprocess.run(
        [sys.executable, "-m", "loopx.cli", *argv],
        cwd=str(worktree), env=fixture["environ"], capture_output=True, text=True, timeout=180,
    )
    payload = json.loads(result.stdout)
    assert payload.get("error") is None, payload.get("error")
    assert result.returncode == 0, result.stderr[-2000:]
    assert (payload["ok"], payload["dry_run"], payload["status"]) == (True, True, "preview")
    # The goal and the pinned todo resolved; the executing Turn would reach the host.
    assert payload["route"]["selected_todo_id"] == todo_id
    assert payload["route"]["would_invoke_host"] is True
    assert payload["effects"]["host_invoked"] is False
