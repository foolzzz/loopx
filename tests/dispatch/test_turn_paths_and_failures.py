"""A Turn finds its goal from any cwd, and a failed Turn says why.

`dispatch serve` handed every Turn the registry path it was given, and the
default is relative (`.loopx/registry.json`). A developer Turn runs in its todo
worktree, so it failed before calling the model with `goal_id not found in
canonical source registry`, and the pass log only recorded `outcome: failed`
with `failure_kind: null`: the reason was left in `runs/<run>.out.json`.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from loopx.cli_commands.dispatch import render_dispatch_pass, render_dispatch_status
from loopx.dispatch import DispatchConfig, Dispatcher, dispatch_status, policy, render_launchd_plist
from loopx.paths import DEFAULT_PROJECT_REGISTRY
from loopx.todos import add_goal_todo
from tests.dispatch.dispatch_fixtures import GOAL_ID, git_env, make_repo, read_jsonl, set_modes, write_fixture
from tests.dispatch.test_loopx_dispatcher import Clock, ScriptedShouldRun, _cli, _dispatcher


def _from_state_home(
    fixture: dict[str, Any], should_run: ScriptedShouldRun, *, runtime_root: Path | None = None,
) -> Dispatcher:
    """A dispatcher given the default relative registry, as `serve` run in the state home is."""

    assert not DEFAULT_PROJECT_REGISTRY.is_absolute()
    config = DispatchConfig(
        registry_path=DEFAULT_PROJECT_REGISTRY,
        runtime_root=runtime_root or fixture["runtime"],
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


def test_a_symlinked_registry_file_keeps_its_link_path_in_the_turn_and_the_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Registry writes replace the link path atomically and lock by path, so a
    # Turn pinned to the old link target would read stale state and skip the locks.
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    home = tmp_path / "linked-home"
    link = home / DEFAULT_PROJECT_REGISTRY
    link.parent.mkdir(parents=True)
    link.symlink_to(fixture["registry"])
    monkeypatch.chdir(home)
    _from_state_home(fixture, ScriptedShouldRun({"dev": ["todo_aaa"]})).run_once()

    [turn] = read_jsonl(fixture["turn_log"])
    assert _option(turn["argv"], "--registry") == str(link)
    plist = plistlib.loads(render_launchd_plist(
        registry_path=DEFAULT_PROJECT_REGISTRY, runtime_root=fixture["runtime"],
        serve_args=["--goal-id", GOAL_ID], environ={"PATH": "/usr/bin"},
    ).encode("utf-8"))
    assert _option(plist["ProgramArguments"], "--registry") == str(link)


def test_a_relative_runtime_root_reaches_the_turn_as_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    monkeypatch.chdir(fixture["project"])
    relative = Path("..") / fixture["runtime"].name
    _from_state_home(fixture, ScriptedShouldRun({"dev": ["todo_aaa"]}), runtime_root=relative).run_once()

    [turn] = read_jsonl(fixture["turn_log"])
    assert _option(turn["argv"], "--runtime-root") == str(fixture["runtime"])


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


def test_a_failed_turn_reports_its_error_and_the_status_shows_the_todo_cooldown(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    set_modes(fixture, {"dev": ["error"]})
    clock = Clock(time.time())  # dispatch status compares cooldowns with the wall clock
    dispatcher = _dispatcher(fixture, should_run=ScriptedShouldRun({"dev": ["todo_aaa"]}), clock=clock)
    launched = dispatcher.run_once(wait=False)["launched"]
    for run in launched:
        dispatcher.children[run["run_id"]].wait(timeout=30)
    report = dispatcher.reconcile()

    [reaped] = report["reaped"]
    assert (reaped["outcome"], reaped["failure_kind"]) == ("failed", None)
    assert reaped["error_code"] == "fixture_goal_unresolved"
    assert reaped["error"].startswith(f"goal_id not found in canonical source registry: {GOAL_ID}")
    # Redacted and bounded: no local path or credential reaches the pass log.
    assert "/Users/someone" not in reaped["error"] and "sk-fixture" not in reaped["error"]
    assert "failed — fixture_goal_unresolved: goal_id not found" in render_dispatch_pass(report)

    clock.now += 61  # past the first backoff: the second failure doubles it
    dispatcher.run_once()
    status = dispatch_status(fixture["runtime"])
    cooldown = status["todo_cooldowns"][f"{GOAL_ID}/todo_aaa@dev"]
    assert cooldown["failures"] == 2 and cooldown["remaining_seconds"] > 0
    [line] = [line for line in render_dispatch_status(status).splitlines() if "todo_aaa" in line]
    until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cooldown["until"]))
    assert "(dev)" in line and f"until {until}" in line and "failures 2" in line
    assert "goal_id not found" in line


def test_failure_text_is_one_redacted_line_of_at_most_300_characters() -> None:
    long = policy.classify_outcome(1, json.dumps({"ok": False, "error": "word\n" * 400, "error_code": "code"}))
    assert long["error_code"] == "code"
    assert len(long["error"]) <= policy.FAILURE_TEXT_MAX_CHARS == 300 and "\n" not in long["error"]
    # A journaled failure carries its reason instead of an error.
    failed = policy.classify_outcome(1, json.dumps({"ok": False, "status": "failed", "reason": "validation_failed"}))
    assert (failed["error"], failed["error_code"]) == ("validation_failed", None)
    committed = policy.classify_outcome(0, json.dumps({"ok": True, "reason": "done"}))
    assert committed["error"] is None


@pytest.mark.parametrize(
    "path",
    [
        "/Volumes/state/providers.yaml",
        "/opt/x/y",
        "C:\\Users\\a\\b",
        "\\\\srv\\share\\f",
        "/Volumes/My Drive/state/providers.yaml",
        "~/work/progress/.loopx/registry.json",
    ],
)
@pytest.mark.parametrize("quote", ["", "'"])
def test_failure_text_masks_any_absolute_path(path: str, quote: str) -> None:
    # The shape of run-once's missing-provider error, which names the runtime root.
    error = f"provider 'p' is not defined in {quote}{path}{quote}; api_key=sk-fixture0123456789 see https://example.com/docs/x"
    text = policy.failure_text(error)
    assert text is not None
    for fragment in (path, *[part for part in path.replace("\\", "/").split("/") if len(part) > 2]):
        assert fragment not in text, text
    assert text.startswith("provider 'p' is not defined in ") and "<path>" in text
    assert "sk-fixture" not in text and "https://example.com/docs/x" in text
