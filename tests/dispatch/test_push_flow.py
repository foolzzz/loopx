"""The push flow (fork gap G8, design decisions 18 and 38).

When a role_v1 goal's work is all merged, the dispatcher opens one
``push_request`` user gate per goal. Approving it pushes each repo's merge
target (plain ``git push``, never force); rejecting records the decision and
waits for new merges. Every remote here is a local bare repo.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from loopx.chat_action_store import ChatActionStore
from loopx.chat_actions import ChatActionService
from loopx.cli import main as cli_main
from loopx.dispatch import DispatchConfig, Dispatcher
from loopx.gate_threads import gate_view, read_gate_index
from loopx.push_requests import (
    PUSH_GATE_TEXT_PREFIX,
    PUSH_LOG_LINE_LIMIT,
    request_push,
    settle_push_gate,
)
from loopx.rollout_event_log import rollout_event_log_path
from loopx.todo_acceptance import accept_goal_todo
from loopx.todos import add_goal_todo, complete_goal_todo, list_goal_todos
from loopx.workspace import git_workspace
from tests.dispatch.dispatch_fixtures import git, git_env, make_repo
from tests.dispatch.test_loopx_dispatcher import Clock, ScriptedShouldRun

GOAL = "g8-goal"
ROLES = {"orch": "orchestrator", "dev": "developer", "acc": "acceptor"}
TASK_BRANCH = f"loopx-task/{GOAL}"


# --- fixture -------------------------------------------------------------------------


def _bare_remote(tmp_path: Path, repo: Path) -> Path:
    bare = tmp_path / "remotes" / f"{repo.name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")
    return bare


def _fixture(tmp_path: Path, monkeypatch, *, model: str = "role_v1", on_push_command: Any = None) -> dict:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "progress"
    home.mkdir()
    state = home / "ACTIVE_GOAL_STATE.md"
    state.write_text("# Goal\n\n## User Todo\n\n## Agent Todo\n\n## Completed Work Archive\n", encoding="utf-8")
    api = make_repo(tmp_path, "api")
    web = make_repo(tmp_path, "web")  # local-only: no remote (G3)
    bare = _bare_remote(tmp_path, api)
    runtime = tmp_path / "runtime"
    coordination: dict = {"agent_model": model, "registered_agents": sorted(ROLES)}
    if model == "role_v1":
        coordination["agent_roles"] = dict(ROLES)
    goal = {
        "id": GOAL, "status": "active", "repo": str(home), "state_file": state.name,
        "repos": [
            {"name": "api", "path": str(api), "default_branch": "main", "merge_target": "task_branch"},
            {"name": "web", "path": str(web), "default_branch": "main", "merge_target": "task_branch"},
        ],
        "coordination": coordination,
    }
    if on_push_command is not None:
        goal["on_push_command"] = on_push_command
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"schema_version": 1, "common_runtime_root": str(runtime), "goals": [goal]}),
                        encoding="utf-8")
    return {"registry": registry, "runtime": runtime, "goal": goal, "api": api, "web": web, "bare": bare,
            "tmp": tmp_path}


def _deliver(fx: dict, repos: list[str], *, name: str = "feature") -> str:
    added = add_goal_todo(registry_path=fx["registry"], goal_id=GOAL, role="agent", text=f"Build {name}",
                          task_class="advancement_task", claimed_by="dev",
                          validation_command_json=json.dumps(["true"]),
                          role_contract={"task_repositories": repos})
    todo_id = str(added["todo_id"])
    paths = git_workspace.prepare(fx["goal"], todo_id, repos, fx["runtime"])["paths"]
    for repo in repos:
        worktree = Path(paths[repo])
        (worktree / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
        git(worktree, "add", ".")
        git(worktree, "commit", "-qm", name)
    delivered = complete_goal_todo(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id, role="agent",
                                   agent_id="dev", evidence="built")
    assert delivered.get("in_review") is True, delivered
    return todo_id


def _accept(fx: dict, todo_id: str) -> None:
    accepted = accept_goal_todo(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id, agent_id="acc")
    assert accepted["merge"]["ok"] is True, accepted


def _merged(fx: dict, repos: list[str] | None = None, *, name: str = "feature") -> str:
    todo_id = _deliver(fx, repos or ["api"], name=name)
    _accept(fx, todo_id)
    return todo_id


def _dispatcher(fx: dict) -> Dispatcher:
    config = DispatchConfig(registry_path=fx["registry"], runtime_root=fx["runtime"], goal_ids=[GOAL],
                            no_global_sync=True, environ={}, loopx_argv=(sys.executable, "-c", "pass"))
    return Dispatcher(config, should_run=ScriptedShouldRun({agent: [] for agent in ROLES}), clock=Clock())


def _push_gates(fx: dict, *, open_only: bool = True) -> list[dict]:
    rows = list_goal_todos(registry_path=fx["registry"], goal_id=GOAL, role="user")["todos"]
    index = read_gate_index(fx["runtime"], GOAL)["gates"]
    return [row for row in rows if (index.get(row["todo_id"]) or {}).get("kind") == "push_request"
            and (row["status"] == "open" or not open_only)]


def _remote_head(fx: dict, branch: str = TASK_BRANCH) -> str | None:
    completed = git_run(fx["bare"], "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    return completed or None


def git_run(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True).stdout.strip()


def _events(fx: dict, kind: str) -> list[dict]:
    path = rollout_event_log_path(fx["runtime"], GOAL)
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("event_kind") == kind]


def _cli(fx: dict, *argv: str) -> tuple[int, dict]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_main(["--registry", str(fx["registry"]), "--runtime-root", str(fx["runtime"]),
                         "--format", "json", *argv])
    return code, json.loads(buffer.getvalue())


def _resolve(fx: dict, gate_id: str, decision: str) -> dict:
    code, payload = _cli(fx, "gate", "resolve", "--goal-id", GOAL, "--todo-id", gate_id, "--decision", decision)
    assert code == 0, payload
    return payload


# --- opening ---------------------------------------------------------------------------


def test_the_gate_opens_once_all_work_is_merged_and_not_while_todos_are_pending(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    dispatcher = _dispatcher(fx)
    first = _merged(fx, name="first")
    pending = _deliver(fx, ["api"], name="second")  # in_review: not all merged yet

    report = dispatcher.run_once()
    assert not [gate for gate in report["gates_opened"] if gate["key"] == "push_request"]
    assert _push_gates(fx) == []
    waiting = request_push(registry_path=fx["registry"], goal_id=GOAL, require_all_merged=True)
    assert waiting["reason"] == "todos_pending" and waiting["pending_todo_ids"] == [pending]

    _accept(fx, pending)
    report = dispatcher.run_once()
    opened = [gate for gate in report["gates_opened"] if gate["key"] == "push_request"]
    assert len(opened) == 1, report
    [gate] = _push_gates(fx)
    assert gate["todo_id"] == opened[0]["todo_id"]
    assert gate["text"].startswith(PUSH_GATE_TEXT_PREFIX)
    assert f"api: {TASK_BRANCH} -> origin (" in gate["text"]
    assert gate.get("blocks_agent") == "orch"
    view = gate_view(registry_path=fx["registry"], runtime_root=fx["runtime"], goal_id=GOAL,
                     todo_id=gate["todo_id"])
    assert view["kind"] == "push_request" and view["push_reason"] == "all_merged"
    [api] = [repo for repo in view["push_repos"] if repo["name"] == "api"]
    head = git(fx["api"], "rev-parse", TASK_BRANCH)
    assert api["status"] == "ready" and api["head"] == head and api["remote"] == "origin"
    assert api["commit_range"].startswith("(new branch)..")
    assert api["goal_merge_commits"] == 2
    assert 0 < len(api["log"]) <= PUSH_LOG_LINE_LIMIT and any("Merge loopx/" in line for line in api["log"])
    assert any(first in line for line in api["log"]) and any(pending in line for line in api["log"])

    # Idempotent: further passes and requests reuse the one open gate.
    again = dispatcher.run_once()
    assert not [gate for gate in again["gates_opened"] if gate["key"] == "push_request"]
    repeated = request_push(registry_path=fx["registry"], goal_id=GOAL)
    assert repeated["opened"] is False and repeated["gate_todo_id"] == gate["todo_id"]
    assert len(_push_gates(fx)) == 1
    assert _remote_head(fx) is None, "opening a gate never pushes"


def test_a_goal_with_nothing_merged_or_only_local_repos_opens_no_gate(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    assert request_push(registry_path=fx["registry"], goal_id=GOAL, require_all_merged=True)["reason"] == (
        "nothing_to_push"
    )
    _merged(fx, ["web"], name="local")  # web has no remote
    result = request_push(registry_path=fx["registry"], goal_id=GOAL, require_all_merged=True)
    assert result["opened"] is False and result["reason"] == "nothing_to_push"
    [web] = [repo for repo in result["repos"] if repo["name"] == "web"]
    assert web["status"] == "no_remote" and "local-only" in web["note"]
    assert _push_gates(fx) == []


def test_the_orchestrator_can_request_a_push_through_the_cli(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    _merged(fx)
    _deliver(fx, ["api"], name="pending")  # an explicit request does not wait for pending todos
    code, payload = _cli(fx, "goal", "request-push", "--goal-id", GOAL, "--agent-id", "dev")
    assert code == 1 and "not the orchestrator" in payload["error"]
    code, payload = _cli(fx, "goal", "request-push", "--goal-id", GOAL, "--agent-id", "orch")
    assert code == 0 and payload["opened"] is True, payload
    entry = read_gate_index(fx["runtime"], GOAL)["gates"][payload["gate_todo_id"]]
    assert entry["push_reason"] == "requested" and entry["requested_by"] == "orch"
    assert [event["details"]["requested_by"] for event in _events(fx, "push_requested")] == ["orch"]


# --- approve, reject, failure ------------------------------------------------------------


def test_approve_pushes_the_merge_target_to_the_bare_remote_and_skips_local_repos(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    _merged(fx, ["api", "web"])
    opened = request_push(registry_path=fx["registry"], goal_id=GOAL, require_all_merged=True)
    assert opened["opened"] is True
    assert "web: skipped, no remote configured (local-only repo)" in opened["gate_text"]
    main_before = git_run(fx["bare"], "rev-parse", "refs/heads/main")

    payload = _resolve(fx, opened["gate_todo_id"], "approve")
    push = payload["push"]
    assert push["ok"] is True and push["pushed"] is True, push
    statuses = {repo["name"]: repo["status"] for repo in push["repos"]}
    assert statuses == {"api": "ok", "web": "skipped"}
    assert _remote_head(fx) == git(fx["api"], "rev-parse", TASK_BRANCH)
    assert git_run(fx["bare"], "rev-parse", "refs/heads/main") == main_before, "main is never pushed"
    [result] = _events(fx, "push_result")
    assert result["status"] == "ok" and result["details"]["repo"] == "api"
    assert result["details"]["branch"] == TASK_BRANCH and result["details"]["remote"] == "origin"
    assert read_gate_index(fx["runtime"], GOAL)["gates"][opened["gate_todo_id"]]["push_outcome"]["pushed"] is True

    # Idempotent: the pushed work is up to date, so nothing reopens, and
    # settling the gate again replays its outcome without pushing.
    assert request_push(registry_path=fx["registry"], goal_id=GOAL)["reason"] == "nothing_to_push"
    # The push is resolved, so the goal_complete gate (decision 42) opens instead.
    assert [gate["key"] for gate in _dispatcher(fx).run_once()["gates_opened"]] == ["goal_complete"]
    replay = settle_push_gate(registry_path=fx["registry"], runtime_root=fx["runtime"], goal_id=GOAL,
                              gate_todo_id=opened["gate_todo_id"], decision="approve")
    assert replay["replayed"] is True
    assert len(_events(fx, "push_result")) == 1
    code, again = _cli(fx, "gate", "resolve", "--goal-id", GOAL, "--todo-id", opened["gate_todo_id"],
                       "--decision", "approve")
    assert code == 1 and "already" in again["error"]


def test_reject_does_not_push_and_waits_for_new_merges(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    dispatcher = _dispatcher(fx)
    _merged(fx, name="first")
    dispatcher.run_once()
    [gate] = _push_gates(fx)
    payload = _resolve(fx, gate["todo_id"], "reject")
    assert payload["push"]["pushed"] is False and payload["push"]["decision"] == "reject"
    assert _remote_head(fx) is None
    [declined] = _events(fx, "push_declined")
    assert declined["status"] == "reject"

    # The same merged work is not offered again (the resolved push lets the
    # goal_complete gate of decision 42 open instead) ...
    assert [gate["key"] for gate in dispatcher.run_once()["gates_opened"]] == ["goal_complete"]
    assert _push_gates(fx) == []
    code, payload = _cli(fx, "goal", "request-push", "--goal-id", GOAL, "--agent-id", "orch")
    assert code == 0 and payload["reason"] == "declined_until_new_merges"
    # ... until a new merge moves the merge target.
    _merged(fx, name="second")
    report = dispatcher.run_once()
    assert [gate["key"] for gate in report["gates_opened"]] == ["push_request"]
    assert _remote_head(fx) is None


def test_cancel_does_not_push(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    _merged(fx)
    opened = request_push(registry_path=fx["registry"], goal_id=GOAL)
    payload = _resolve(fx, opened["gate_todo_id"], "cancel")
    assert payload["push"]["pushed"] is False
    assert _remote_head(fx) is None
    assert request_push(registry_path=fx["registry"], goal_id=GOAL, require_all_merged=True)["reason"] == (
        "declined_until_new_merges"
    )


def test_a_failed_push_leaves_a_follow_up_gate_with_the_error(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    hook = fx["bare"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'fixture remote refuses pushes' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _merged(fx)
    opened = request_push(registry_path=fx["registry"], goal_id=GOAL)
    payload = _resolve(fx, opened["gate_todo_id"], "approve")
    push = payload["push"]
    assert push["ok"] is False and push["pushed"] is False
    [api] = [repo for repo in push["repos"] if repo["name"] == "api"]
    assert api["status"] == "error" and "fixture remote refuses pushes" in api["error_tail"]
    assert str(fx["bare"]) not in api["error_tail"], "local paths are redacted"
    [result] = _events(fx, "push_result")
    assert result["status"] == "error"
    [follow_up] = _push_gates(fx)
    assert follow_up["todo_id"] == push["follow_up_gate_todo_id"] != opened["gate_todo_id"]
    assert "Last push failed" in follow_up["text"] and "fixture remote refuses pushes" in follow_up["text"]
    entry = read_gate_index(fx["runtime"], GOAL)["gates"][follow_up["todo_id"]]
    assert entry["push_reason"] == "push_failed" and entry["previous_errors"][0]["name"] == "api"
    assert _remote_head(fx) is None

    # Once the remote accepts pushes, approving the follow-up gate pushes.
    hook.unlink()
    retried = _resolve(fx, follow_up["todo_id"], "approve")["push"]
    assert retried["ok"] is True and retried["pushed"] is True
    assert _remote_head(fx) == git(fx["api"], "rev-parse", TASK_BRANCH)
    assert _push_gates(fx) == []


def test_a_rejected_non_fast_forward_is_never_forced(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    _merged(fx)
    # Someone else put a different history on the remote task branch.
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(fx["bare"]), str(other))
    (other / "other.txt").write_text("other\n", encoding="utf-8")
    git(other, "checkout", "-q", "-b", TASK_BRANCH)
    git(other, "add", ".")
    git(other, "commit", "-qm", "other history")
    git(other, "push", "-q", "origin", TASK_BRANCH)
    theirs = git(other, "rev-parse", "HEAD")
    opened = request_push(registry_path=fx["registry"], goal_id=GOAL)
    push = _resolve(fx, opened["gate_todo_id"], "approve")["push"]
    assert push["ok"] is False and push["repos"][0]["status"] == "error"
    assert _remote_head(fx) == theirs
    assert push.get("follow_up_gate_todo_id")


def test_new_merges_after_the_gate_opened_are_not_pushed_unapproved(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    dispatcher = _dispatcher(fx)
    _merged(fx, name="first")
    opened = request_push(registry_path=fx["registry"], goal_id=GOAL)
    _merged(fx, name="second")
    push = _resolve(fx, opened["gate_todo_id"], "approve")["push"]
    assert push["repos"][0]["status"] == "head_moved" and push["pushed"] is False
    assert _remote_head(fx) is None
    # The dispatcher offers the whole merged work again.
    assert [gate["key"] for gate in dispatcher.run_once()["gates_opened"]] == ["push_request"]


def test_an_approval_that_is_not_recorded_in_state_never_pushes(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    _merged(fx)
    opened = request_push(registry_path=fx["registry"], goal_id=GOAL)
    # Settling without closing the gate todo through the decision path.
    outcome = settle_push_gate(registry_path=fx["registry"], runtime_root=fx["runtime"], goal_id=GOAL,
                               gate_todo_id=opened["gate_todo_id"], decision="approve")
    assert outcome["ok"] is False and "not recorded" in outcome["error"]
    assert _remote_head(fx) is None


def test_on_push_command_runs_in_the_repo_after_a_successful_push(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "on-push.txt"
    fx = _fixture(tmp_path, monkeypatch, on_push_command=[
        sys.executable, "-c",
        f"import pathlib, subprocess; pathlib.Path({str(marker)!r}).write_text("
        "subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], text=True))",
    ])
    _merged(fx)
    opened = request_push(registry_path=fx["registry"], goal_id=GOAL)
    push = _resolve(fx, opened["gate_todo_id"], "approve")["push"]
    [api] = [repo for repo in push["repos"] if repo["name"] == "api"]
    assert api["status"] == "ok" and api["on_push_command"]["ok"] is True
    assert marker.read_text(encoding="utf-8").strip() == "main"  # ran in the api checkout
    assert _events(fx, "push_result")[0]["details"]["on_push_command_ok"] is True


# --- web --------------------------------------------------------------------------------------


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_the_web_resolve_path_pushes_on_approve_only(tmp_path, monkeypatch, decision: str) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    _merged(fx)
    opened = request_push(registry_path=fx["registry"], goal_id=GOAL)
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=fx["registry"])
    proposal = service.preview({
        "action_kind": "gate.resolve", "summary": f"{decision} the push",
        "normalized_parameters": {"goal_id": GOAL, "todo_id": opened["gate_todo_id"], "decision": decision},
        "context": {}, "idempotency_key": f"g8-{decision}",
    })
    assert proposal["status"] == "preview_ready", proposal
    assert _remote_head(fx) is None, "the preview is a dry run"
    applied = service.apply(proposal["proposal_id"])["proposal"]
    assert applied["status"] == "applied", applied
    if decision == "approve":
        assert _remote_head(fx) == git(fx["api"], "rev-parse", TASK_BRANCH)
        assert [event["status"] for event in _events(fx, "push_result")] == ["ok"]
    else:
        assert _remote_head(fx) is None
        assert [event["status"] for event in _events(fx, "push_declined")] == ["reject"]


# --- peer_v1 ------------------------------------------------------------------------------------


def test_peer_v1_goals_are_unaffected(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch, model="peer_v1")
    added = add_goal_todo(registry_path=fx["registry"], goal_id=GOAL, role="agent", text="Build it",
                          task_class="advancement_task", claimed_by="dev",
                          role_contract={"task_repositories": ["api"]}, validation_command_json='["true"]')
    todo_id = str(added["todo_id"])
    worktree = Path(git_workspace.prepare(fx["goal"], todo_id, None, fx["runtime"])["paths"]["api"])
    (worktree / "x.txt").write_text("x\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", "x")
    assert git_workspace.merge(fx["goal"], todo_id, ["api"], fx["runtime"])["ok"] is True
    complete_goal_todo(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id, role="agent",
                       agent_id="dev", evidence="built")
    report = _dispatcher(fx).run_once()
    assert report["gates_opened"] == [] and not [e for e in report["errors"] if "push" in e["error"]]
    assert _push_gates(fx) == []
    refused = request_push(registry_path=fx["registry"], goal_id=GOAL)
    assert refused["ok"] is False and refused["reason"] == "not_role_v1"
    assert _events(fx, "push_requested") == []
