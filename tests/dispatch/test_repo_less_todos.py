"""Todos that name no code repository (docs/fork/usage.md, "Goals without a code repository").

``loopx goal create`` without ``--repo`` records the project directory as the
goal's implicit repo ``main``. A todo without ``task_repositories`` has no
per-todo workspace: its developer and acceptor Turns run in, and deliver from,
the goal's project directory. These tests pin the honest limits of that mode:

* such a Turn settles from the project directory (git or not), while a todo
  that names repos is still refused there;
* the dispatcher runs at most one repo-less developer or acceptor Turn per
  goal at a time, because nothing isolates them from each other;
* the goal_complete gate opens for a non-git project directory, which has
  nothing to push.

Turns use a no-model ``generic-cli`` host and dispatcher passes a fake
``turn run-once``; should-run, writeback and the gates are real.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from loopx.cli import main as cli_main
from loopx.cli_commands.dispatch import render_dispatch_status
from loopx.control_plane.agents.workspace_guard import (
    PeerDeliveryWorkspace,
    build_agent_workspace_guard,
    peer_delivery_workspace,
)
from loopx.dispatch import DispatchConfig, Dispatcher, dispatch_status
from loopx.dispatch.state import load_state
from loopx.event_sourced_state import TODO_ADDED, AppendOnlyStateEventStore, make_state_event
from loopx.goal_complete_gate import goal_completion_snapshot
from loopx.push_requests import repo_push_plan
from loopx.state_refresh import refresh_state_run
from loopx.todo_acceptance import accept_goal_todo
from loopx.todos import add_goal_todo, complete_goal_todo, list_goal_todos
from tests.dispatch.dispatch_fixtures import GOAL_ID, git, git_env, make_repo, set_modes, write_fixture
from tests.dispatch.test_loopx_dispatcher import Clock, _release_and_wait

ROOT = Path(__file__).resolve().parents[2]
GOAL = "norepo"
DELIVERY_REFUSED = "accountable peer delivery must be refreshed from the independent git worktree"
PROJECT_DIRECTORY_TURN_RUNNING = "project_directory_turn_running"
PROJECT_DIRECTORY_REFUSED = "must be refreshed from the goal's project directory"
ROLES = {"orch": "orchestrator", "dev": "developer", "acc": "acceptor"}

# The generic-cli host: no model. It writes NOTES.md in its cwd and reports
# a validated completion (a developer delivery, or an acceptor's accept).
STUB_HOST = r'''
import json, os, sys
from pathlib import Path
from loopx.control_plane.turn_driver.host_candidate import COMPLETED_PHASES, LOOPX_TURN_RESULT_SCHEMA
request = json.load(sys.stdin)
if os.environ.get("STUB_WRITE", "1") == "1":
    Path("NOTES.md").write_text("# Roles\n\n- Orchestrator plans.\n- Developer builds.\n- Acceptor reviews.\n")
print(json.dumps({
    "schema_version": LOOPX_TURN_RESULT_SCHEMA, "turn_key": request.get("turn_key", ""),
    "result_kind": "validated_completion", "completed_phases": list(COMPLETED_PHASES),
    "classification": "notes_written", "summary": "NOTES.md holds the three roles.",
    "next_action": "Review NOTES.md.", "recommended_action": "Review NOTES.md against the criteria.",
    "delivery_batch_scale": "single_surface", "delivery_outcome": "primary_goal_outcome",
}))
'''


def _goal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, git_project: bool) -> dict[str, Any]:
    """A role_v1 goal made by ``goal create`` without ``--repo``."""

    home = tmp_path / "home"
    home.mkdir()
    env = {**git_env(tmp_path), "HOME": str(home)}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    project = tmp_path / "project"
    project.mkdir()
    if git_project:
        git(project, "init", "-q", "-b", "main")
        (project / "README.md").write_text("project\n", encoding="utf-8")
        git(project, "add", ".")
        git(project, "commit", "-qm", "init")
    doc = tmp_path / "requirements.md"
    doc.write_text("# Summarize the LoopX roles in NOTES.md\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_main([
            "--runtime-root", str(runtime), "--format", "json", "goal", "create", "--project", str(project),
            "--goal-id", GOAL, "--doc", str(doc), "--agent", "orch=orchestrator", "--agent", "dev=developer",
            "--agent", "acc=acceptor", "--no-global-sync",
        ])
    assert code == 0, out.getvalue()
    stub = tmp_path / "stub_host.py"
    stub.write_text(STUB_HOST, encoding="utf-8")
    return {
        "project": project, "runtime": runtime, "registry": Path(json.loads(out.getvalue())["registry"]),
        "stub": stub, "environ": {**os.environ, **env, "PYTHONPATH": str(ROOT)},
    }


def _add_todo(
    fx: dict[str, Any], text: str, *, repos: list[str] | None = None, goal_id: str = GOAL, **fields: Any,
) -> str:
    added = add_goal_todo(
        registry_path=fx["registry"], goal_id=goal_id, role="agent", text=text, claimed_by="dev",
        runtime_root_arg=str(fx["runtime"]), **fields,
        role_contract={"required_role": "developer", **({"task_repositories": repos} if repos else {})},
    )
    return str(added["todo_id"])


def _todo(fx: dict[str, Any], todo_id: str) -> dict[str, Any]:
    listed = list_goal_todos(registry_path=fx["registry"], goal_id=GOAL, runtime_root_arg=str(fx["runtime"]))
    return next(row for row in listed["todos"] if row["todo_id"] == todo_id)


def _run_once(
    fx: dict[str, Any], agent_id: str, todo_id: str, *, project: Path | None = None,
    turn_instance_id: str | None = None, **env: str,
) -> dict[str, Any]:
    """``turn run-once`` with the dispatcher's arguments, from the goal's project directory by default."""

    project = project or fx["project"]
    argv = [
        sys.executable, "-m", "loopx.cli", "--registry", str(fx["registry"]), "--runtime-root", str(fx["runtime"]),
        "--format", "json", "turn", "run-once", "--goal-id", GOAL, "--agent-id", agent_id,
        "--project", str(project), "--host", "generic-cli",
        "--host-adapter-command-json", json.dumps([sys.executable, str(fx["stub"])]),
        "--timeout-seconds", "120", "--execute",
        "--turn-instance-id", turn_instance_id or f"dispatch-{uuid.uuid4().hex[:12]}",
        "--todo-id", todo_id, "--validation-command-json", json.dumps([sys.executable, "-c", "pass"]),
        "--no-global-sync",
    ]
    completed = subprocess.run(
        argv, cwd=str(project), env={**fx["environ"], **env}, capture_output=True, text=True, timeout=180,
    )
    try:
        return json.loads(completed.stdout)
    except ValueError:
        raise AssertionError(f"run-once printed no JSON: {completed.stdout[-2000:]} {completed.stderr[-2000:]}")


# --- writeback from the project directory ----------------------------------------------


@pytest.mark.parametrize(
    ("git_project", "action_kind"),
    [(False, None), (True, None), (False, "implement")],
    ids=["plain-directory", "git-directory", "plain-directory-implement"],
)
def test_a_repo_less_todo_is_delivered_and_accepted_from_the_project_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_project: bool, action_kind: str | None,
) -> None:
    # ``implement`` is write work, which should-run guards with the same rule.
    fx = _goal(tmp_path, monkeypatch, git_project=git_project)
    todo_id = _add_todo(fx, "Write NOTES.md naming the three LoopX roles",
                        **({"action_kind": action_kind} if action_kind else {}))

    delivered = _run_once(fx, "dev", todo_id)
    assert delivered.get("ok") is True, json.dumps(delivered)[:3000]
    assert delivered["status"] == "committed"
    assert (fx["project"] / "NOTES.md").is_file()
    assert _todo(fx, todo_id)["status"] == "in_review"

    accepted = _run_once(fx, "acc", todo_id, STUB_WRITE="0")
    assert accepted.get("ok") is True, json.dumps(accepted)[:3000]
    row = _todo(fx, todo_id)
    assert row["status"] == "done" and "accepted_by=acc" in row["evidence"], row


def test_a_todo_that_names_a_repo_is_still_refused_from_the_project_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The project directory is the implicit repo ``main``, so a todo may name
    # it; its delivery must still come from its own per-todo worktree.
    fx = _goal(tmp_path, monkeypatch, git_project=True)
    todo_id = _add_todo(fx, "Change README.md in repo main", repos=["main"])

    refused = _run_once(fx, "dev", todo_id)
    assert refused.get("ok") is False, json.dumps(refused)[:3000]
    assert DELIVERY_REFUSED in str(refused.get("error") or refused.get("reason")), json.dumps(refused)[:3000]


def test_a_callers_project_never_widens_the_delivery_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The state file is absolute in the registry, so a refresh with another
    # --project still reads and writes the real goal state.
    fx = _goal(tmp_path, monkeypatch, git_project=False)
    registry = json.loads(fx["registry"].read_text(encoding="utf-8"))
    [goal] = registry["goals"]
    goal["state_file"] = str((fx["project"] / goal["state_file"]).resolve())
    fx["registry"].write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    todo_id = _add_todo(fx, "Write NOTES.md naming the three LoopX roles")
    outside = tmp_path / "outside"
    outside.mkdir()
    turn = f"dispatch-{uuid.uuid4().hex[:12]}"
    refused = _run_once(fx, "dev", todo_id, project=outside, turn_instance_id=turn)
    assert refused.get("ok") is False and PROJECT_DIRECTORY_REFUSED in str(refused.get("error")), refused

    # Retry that Turn's writeback by hand, pointing --project at the outside directory.
    with pytest.raises(ValueError, match=PROJECT_DIRECTORY_REFUSED):
        refresh_state_run(
            registry_path=fx["registry"], runtime_root_override=str(fx["runtime"]), goal_id=GOAL,
            project=outside, state_file=None, classification="notes_written",
            recommended_action="Review NOTES.md against the criteria.", next_action="Review NOTES.md.",
            delivery_batch_scale="single_surface", delivery_outcome="primary_goal_outcome",
            delivery_workspace_path=outside, todo_id=todo_id, turn_instance_id=turn, agent_id="dev",
            progress_scope="goal", completion_todo_id=todo_id, completion_turn_key=turn,
            dry_run=False, sync_global=False,
        )


def test_todo_sources_that_disagree_about_repos_keep_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Markdown block names no repo, but the goal's event projection of the
    # same todo does. should-run reads the projection and settlement the block;
    # a stale source must never relax a todo that names a repo.
    fx = _goal(tmp_path, monkeypatch, git_project=True)
    todo_id = _add_todo(fx, "Write NOTES.md naming the three LoopX roles")
    [goal] = json.loads(fx["registry"].read_text(encoding="utf-8"))["goals"]
    AppendOnlyStateEventStore((fx["project"] / goal["state_file"]).with_name("events.jsonl")).append_many([
        make_state_event(
            event_id="add-notes", goal_id=GOAL, event_type=TODO_ADDED, refs={"todo_id": todo_id},
            payload={"role": "agent", "priority": "P1", "text": "Write NOTES.md naming the three LoopX roles",
                     "task_class": "advancement_task", "claimed_by": "dev",
                     "task_repository": "https://example.test/fixture/notes.git"},
        ),
    ])

    refused = _run_once(fx, "dev", todo_id)
    assert refused.get("ok") is False, json.dumps(refused)[:3000]
    assert DELIVERY_REFUSED in str(refused.get("error")), json.dumps(refused)[:3000]


REPO_LESS = {"todo_id": "todo_notes", "action_kind": "implement"}
NAMED = {**REPO_LESS, "task_repositories": ["main"]}
UPSTREAM = {**REPO_LESS, "task_repository": "https://example.test/fixture/api.git"}


def _role_goal(project: Path, model: str = "role_v1", **extra: Any) -> dict[str, Any]:
    coordination: dict[str, Any] = {"agent_model": model, "registered_agents": sorted(ROLES)}
    if model == "role_v1":
        coordination["agent_roles"] = dict(ROLES)
    return {"id": GOAL, "repo": str(project), "coordination": coordination, **extra}


def test_the_delivery_workspace_rule_is_relaxed_only_for_role_v1_todos_without_repos(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    repo_less, named, upstream = REPO_LESS, NAMED, UPSTREAM

    def goal(model: str = "role_v1", **extra: Any) -> dict[str, Any]:
        return _role_goal(project, model, **extra)

    def rule(agent: str, todo: dict[str, Any] | None, *, multi_agent: bool = True, **goal_kwargs: Any):
        return peer_delivery_workspace(goal(**goal_kwargs), agent_id=agent, multi_agent=multi_agent, todo=todo)

    assert rule("dev", repo_less) is PeerDeliveryWorkspace.GOAL_PROJECT_DIRECTORY
    assert rule("acc", repo_less) is PeerDeliveryWorkspace.GOAL_PROJECT_DIRECTORY
    assert rule("orch", repo_less) is PeerDeliveryWorkspace.ANY
    for agent in ROLES:  # a todo with repos, or an unknown one, keeps the guard for every role
        for todo in (named, upstream, None):
            assert rule(agent, todo) is PeerDeliveryWorkspace.INDEPENDENT_WORKTREE, (agent, todo)
    assert rule("dev", repo_less, model="peer_v1") is PeerDeliveryWorkspace.INDEPENDENT_WORKTREE
    required = {"workspace_guard_policy": {"peer_independent_worktree_required": True}}
    assert rule("dev", repo_less, **required) is PeerDeliveryWorkspace.INDEPENDENT_WORKTREE
    assert rule("dev", named, multi_agent=False) is PeerDeliveryWorkspace.ANY

    # should-run's workspace guard asks the same rule.
    identity = {"agent_id": "dev", "registered_agents": sorted(ROLES)}
    assert build_agent_workspace_guard(goal(), identity, selected_todo=repo_less, current_path=project) is None
    guard = build_agent_workspace_guard(goal(), identity, selected_todo=named, current_path=project)
    assert guard is not None and guard["action"] == "move_to_independent_worktree"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert build_agent_workspace_guard(goal(), identity, selected_todo=repo_less, current_path=elsewhere)


def test_an_orchestrator_todo_with_repos_is_still_guarded_at_should_run(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    identity = {"agent_id": "orch", "registered_agents": sorted(ROLES)}
    guard = build_agent_workspace_guard(_role_goal(project), identity, selected_todo=NAMED, current_path=project)
    assert guard is not None and guard["action"] == "move_to_independent_worktree", guard
    # The orchestrator's own todos name no repo and stay exempt.
    assert build_agent_workspace_guard(
        _role_goal(project), identity, selected_todo=REPO_LESS, current_path=project,
    ) is None


# --- one project-directory Turn per goal ---------------------------------------------------


def test_the_dispatcher_runs_one_project_directory_turn_per_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    fixture = write_fixture(
        tmp_path, agents={"dev": {"role": "developer", "max_concurrency": 3}, "acc": {"role": "acceptor"}},
        repos={"api": make_repo(tmp_path, "api")},
    )
    review = _add_todo(fixture, "Draft NOTES.md", goal_id=GOAL_ID, priority="P0")
    delivered = complete_goal_todo(registry_path=fixture["registry"], goal_id=GOAL_ID, todo_id=review, role="agent",
                                   agent_id="dev", evidence="drafted", runtime_root_arg=str(fixture["runtime"]))
    assert delivered.get("in_review") is True, delivered
    first = _add_todo(fixture, "Write GLOSSARY.md", goal_id=GOAL_ID, priority="P0")
    second = _add_todo(fixture, "Write FAQ.md", goal_id=GOAL_ID, priority="P1")
    with_repo = _add_todo(fixture, "Build the api", goal_id=GOAL_ID, priority="P2", repos=["api"])
    set_modes(fixture, {"dev": ["hold"], "acc": ["hold"]})
    dispatcher = Dispatcher(DispatchConfig(
        registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_ids=[GOAL_ID], no_global_sync=True,
        environ=fixture["environ"], loopx_argv=(sys.executable, str(fixture["fake_loopx"])),
    ), clock=Clock())
    try:
        report = dispatcher.reconcile()
        # The acceptor's review of the repo-less delivery holds the project
        # directory; the developer's free slot takes the todo with a repo.
        launched = {(item["agent_id"], item["todo_id"]) for item in report["launched"]}
        assert launched == {("acc", review), ("dev", with_repo)}, json.dumps({k: report[k] for k in ("launched", "skipped", "errors")})
        workspaces = {run["todo_id"]: run.get("workspace") for run in load_state(fixture["runtime"])["runs"].values()}
        assert workspaces == {review: "project_directory", with_repo: "todo_workspace"}
        [wait] = [item for item in report["skipped"] if item["reason"] == PROJECT_DIRECTORY_TURN_RUNNING]
        assert wait["agent_id"] == "dev" and wait["todo_id"] in {first, second}
        assert wait["running_todo_id"] == review
        status = dispatch_status(fixture["runtime"])
        assert status["last_pass"]["project_directory_waits"] == [wait]
        text = render_dispatch_status(status)
        assert f"todo {review} pid" in text and "in the project directory" in text
        assert f"{PROJECT_DIRECTORY_TURN_RUNNING} (todo {review})" in text

        again = dispatcher.reconcile()
        assert again["launched"] == []
        assert PROJECT_DIRECTORY_TURN_RUNNING in {item["reason"] for item in again["skipped"]}
    finally:
        _release_and_wait(fixture, dispatcher)
    # With the directory free again, one repo-less Turn takes it.
    report = dispatcher.reconcile()
    project_runs = [run for run in load_state(fixture["runtime"])["runs"].values()
                    if run.get("workspace") == "project_directory"]
    assert len(project_runs) == 1, report
    _release_and_wait(fixture, dispatcher)


class _DispatcherDied(BaseException):
    """Stands in for the dispatcher process dying while it starts a Turn."""


def test_a_run_is_recorded_before_its_turn_starts_and_a_restart_reclaims_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer", "max_concurrency": 2}})
    first = _add_todo(fixture, "Write GLOSSARY.md", goal_id=GOAL_ID, priority="P0")
    _add_todo(fixture, "Write FAQ.md", goal_id=GOAL_ID, priority="P1")
    set_modes(fixture, {"dev": ["hold"]})
    config = DispatchConfig(
        registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_ids=[GOAL_ID], no_global_sync=True,
        environ=fixture["environ"], loopx_argv=(sys.executable, str(fixture["fake_loopx"])),
    )
    persisted_at_start: list[dict[str, Any]] = []
    real_popen = subprocess.Popen

    def dies_while_starting(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if "run-once" not in argv:  # the auth preflight
            return real_popen(argv, *args, **kwargs)
        persisted_at_start.extend(load_state(fixture["runtime"]).get("runs", {}).values())
        raise _DispatcherDied()

    with monkeypatch.context() as patch:
        patch.setattr("loopx.dispatch.dispatcher.subprocess.Popen", dies_while_starting)
        with pytest.raises(_DispatcherDied):
            Dispatcher(config, clock=Clock()).reconcile()
    # The project-directory slot was already on disk when the child started.
    [started] = persisted_at_start
    assert started["todo_id"] == first and started["workspace"] == "project_directory"
    assert started["pid"] is None

    restarted = Dispatcher(config, clock=Clock())
    try:
        report = restarted.reconcile()
        # No process holds it, so the reap settles it as a crash and one
        # project-directory Turn runs again, under the same identity.
        [reaped] = report["reaped"]
        assert reaped["todo_id"] == first and reaped["outcome"] == "crashed"
        runs = list(load_state(fixture["runtime"])["runs"].values())
        assert [(run["todo_id"], run["workspace"]) for run in runs] == [(first, "project_directory")]
        assert runs[0]["turn_instance_id"] == started["turn_instance_id"] and isinstance(runs[0]["pid"], int)
    finally:
        _release_and_wait(fixture, restarted)


# --- goal_complete ---------------------------------------------------------------------------


@pytest.mark.parametrize("no_commit_git", [False, True], ids=["plain-directory", "git-init-without-commit"])
def test_goal_complete_opens_for_a_project_directory_with_nothing_to_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_commit_git: bool,
) -> None:
    fx = _goal(tmp_path, monkeypatch, git_project=False)
    if no_commit_git:
        git(fx["project"], "init", "-q", "-b", "main")  # an unborn HEAD
    api = {"registry_path": fx["registry"], "goal_id": GOAL, "runtime_root_arg": str(fx["runtime"])}
    [plan] = [row["todo_id"] for row in list_goal_todos(**api)["todos"] if row.get("claimed_by") == "orch"]
    assert complete_goal_todo(**api, todo_id=plan, role="agent", agent_id="orch", evidence="planned")["ok"]
    todo_id = _add_todo(fx, "Write NOTES.md naming the three LoopX roles")
    assert complete_goal_todo(**api, todo_id=todo_id, role="agent", agent_id="dev", evidence="written")["in_review"]
    assert accept_goal_todo(**api, todo_id=todo_id, agent_id="acc")["ok"]

    snapshot = goal_completion_snapshot(registry_path=fx["registry"], runtime_root=fx["runtime"], goal_id=GOAL)
    assert snapshot["complete"] is True, snapshot
    assert [(repo["name"], repo["push"]) for repo in snapshot["repos"]] == [("main", "local_only")]
    dispatcher = Dispatcher(DispatchConfig(
        registry_path=fx["registry"], runtime_root=fx["runtime"], goal_ids=[GOAL], no_global_sync=True,
        environ=fx["environ"], loopx_argv=(sys.executable, "-c", "raise SystemExit(3)"),
    ), should_run=lambda _goal_id, _agent_id: {"should_run": False}, clock=Clock())
    opened = dispatcher.reconcile()["gates_opened"]
    assert [item["key"] for item in opened] == ["goal_complete"]

    # Only the implicit repo of a goal without --repo is skipped this way; a
    # declared repo in the same state is still an error.
    declared = {"name": "docs", "path": str(fx["project"]), "merge_target": "main"}
    assert repo_push_plan({"id": GOAL, "repos": [declared]}, declared, GOAL)["status"] == "error"
