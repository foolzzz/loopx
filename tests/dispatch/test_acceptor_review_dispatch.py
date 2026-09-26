"""Fork gap G12 through the dispatcher and a real `turn run-once`.

An acceptor Turn runs in a detached review checkout of the delivered commit,
the checkout is inspected and removed afterwards, and the Turn's result maps
to the accept / reject / blocked verdicts. A fake `claude` stands in for the
model; everything else (should-run, run-once, writeback) is real.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from loopx.dispatch import DispatchConfig, Dispatcher
from loopx.dispatch.state import load_state
from loopx.rollout_event_log import rollout_event_log_path
from loopx.todos import add_goal_todo, complete_goal_todo, list_goal_todos
from loopx.workspace import git_workspace
from tests.dispatch.dispatch_fixtures import GOAL_ID, git, git_env, make_repo, read_jsonl, write_fixture

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _delivered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], str, str]:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    api = make_repo(tmp_path, "api")
    # Multi-agent deliveries bind to a credential-free repository identity.
    git(api, "remote", "add", "origin", "https://example.test/fixture/api.git")
    fixture = write_fixture(
        tmp_path,
        agents={"dev": {"role": "developer"}, "acc": {"role": "acceptor"}},
        repos={"api": api},
    )
    fixture["api"] = api
    api_kwargs = {"registry_path": fixture["registry"], "goal_id": GOAL_ID, "runtime_root_arg": str(fixture["runtime"])}
    added = add_goal_todo(**api_kwargs, role="agent", text="Build the api fixture", priority="P0",
                          task_class="advancement_task", claimed_by="dev",
                          validation_command_json=json.dumps(["true"]),
                          role_contract={"task_repositories": ["api"]})
    todo_id = str(added["todo_id"])
    goal = json.loads(fixture["registry"].read_text())["goals"][0]
    worktree = Path(git_workspace.prepare(goal, todo_id, None, fixture["runtime"])["paths"]["api"])
    (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", "feature")
    delivered = complete_goal_todo(**api_kwargs, todo_id=todo_id, role="agent", agent_id="dev", evidence="built")
    assert delivered.get("in_review") is True, delivered
    return fixture, todo_id, git(worktree, "rev-parse", "HEAD")


def _dispatcher(fixture: dict[str, Any], **env: str) -> Dispatcher:
    config = DispatchConfig(
        registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_ids=[GOAL_ID],
        no_global_sync=True, environ={**fixture["environ"], "PYTHONPATH": str(ROOT), **env},
        turn_timeout_seconds=120,
    )
    return Dispatcher(config, clock=Clock())


def _todo(fixture: dict[str, Any], todo_id: str) -> dict[str, Any]:
    listed = list_goal_todos(registry_path=fixture["registry"], goal_id=GOAL_ID,
                             runtime_root_arg=str(fixture["runtime"]))
    return next(row for row in listed["todos"] if row["todo_id"] == todo_id)


def _events(fixture: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    path = rollout_event_log_path(fixture["runtime"], GOAL_ID)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("event_kind") == kind]


def _run_acceptor(fixture: dict[str, Any], todo_id: str, **env: str) -> dict[str, Any]:
    dispatcher = _dispatcher(fixture, **env)
    report = dispatcher.run_once()
    launched = [(item["agent_id"], item["todo_id"]) for item in report["launched"]]
    assert launched == [("acc", todo_id)], report
    finished = report["finished"][0]
    payload = json.loads(Path(load_state(fixture["runtime"])["history"][-1]["stdout_path"]).read_text())
    assert finished["outcome"] == "committed", json.dumps(payload)[:3000]
    return {"report": report, "payload": payload, "dispatcher": dispatcher}


def test_acceptor_turn_runs_in_a_detached_review_checkout_and_accept_merges_the_delivered_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, todo_id, sha = _delivered(tmp_path, monkeypatch)
    run = _run_acceptor(fixture, todo_id, FAKE_CLAUDE_RESULT_KIND="validated_completion",
                        FAKE_CLAUDE_SUMMARY="Every criterion holds.")
    [call] = read_jsonl(fixture["claude_log"])
    cwd = Path(call["cwd"]).resolve()
    reviews = (fixture["runtime"] / "goals" / GOAL_ID / "reviews" / todo_id).resolve()
    assert cwd.parent.parent == reviews and cwd.name == "api"
    assert "review checkout of the delivered commit" in call["system_prompt"]
    assert "never modify code" in call["system_prompt"]
    assert not cwd.exists(), "the review checkout is removed after the Turn"
    assert "/reviews/" not in git(fixture["api"], "worktree", "list")  # only the dev worktree remains
    # The fake model wrote an untracked file into its checkout: warned, discarded, verdict stands.
    [warning] = _events(fixture, "acceptor_modified_review_checkout")
    assert warning["todo_id"] == todo_id and warning["agent_id"] == "acc"
    assert load_state(fixture["runtime"])["review_warnings"][f"{GOAL_ID}/{todo_id}"]["repos"] == ["api"]
    todo = _todo(fixture, todo_id)
    assert todo["status"] == "done", run["payload"]
    assert "accepted_by=acc" in todo["evidence"]
    assert git(fixture["api"], "merge-base", "--is-ancestor", sha, "main") == ""
    assert git(fixture["api"], "show", "main:feature.txt") == "feature"
    assert "fixture-artifact.txt" not in git(fixture["api"], "ls-tree", "--name-only", "main")


def test_an_acceptor_user_action_required_turn_opens_the_blocked_gate_and_is_not_relaunched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, todo_id, _sha = _delivered(tmp_path, monkeypatch)
    run = _run_acceptor(fixture, todo_id, FAKE_CLAUDE_RESULT_KIND="user_action_required",
                        FAKE_CLAUDE_SUMMARY="The review env has no node toolchain.")
    acceptance = run["payload"]["acceptance"]
    assert acceptance["verdict"] == "blocked", run["payload"]
    todo = _todo(fixture, todo_id)
    assert todo["status"] == "in_review" and not todo.get("reject_count")
    gate = _todo(fixture, acceptance["gate_todo_id"])
    assert gate["status"] == "open" and "no node toolchain" in gate["text"]

    # While the gate is open the acceptor is not relaunched on the todo.
    again = _dispatcher(fixture).run_once()
    assert again["launched"] == []
    skipped = {item["agent_id"]: item["reason"] for item in again["skipped"]}
    assert skipped["acc"] in {"should_run_false", "review_blocked_gate_open"}, again
    assert len(read_jsonl(fixture["claude_log"])) == 1


def test_an_acceptor_repair_required_without_feedback_becomes_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, todo_id, _sha = _delivered(tmp_path, monkeypatch)
    _run_acceptor(fixture, todo_id, FAKE_CLAUDE_RESULT_KIND="repair_required", FAKE_CLAUDE_SUMMARY="")
    todo = _todo(fixture, todo_id)
    assert todo["status"] == "in_review" and not todo.get("reject_count")
    assert len(_events(fixture, "todo_review_blocked")) == 1


def test_the_dispatcher_skips_a_todo_with_an_open_blocked_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loopx.todo_review_blocked import block_goal_todo_review

    fixture, todo_id, _sha = _delivered(tmp_path, monkeypatch)
    block_goal_todo_review(registry_path=fixture["registry"], goal_id=GOAL_ID, todo_id=todo_id, agent_id="acc",
                           reason="broken tooling", runtime_root_arg=str(fixture["runtime"]))

    def should_run(goal_id: str, agent_id: str) -> dict[str, Any]:
        # Even if should-run offered the todo, the dispatcher must not relaunch the acceptor on it.
        if agent_id != "acc":
            return {"should_run": False}
        return {"should_run": True, "effective_action": "normal_run",
                "selected_todo": {"todo_id": todo_id, "role": "agent"}}

    config = DispatchConfig(registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_ids=[GOAL_ID],
                            no_global_sync=True, environ=fixture["environ"],
                            loopx_argv=(sys.executable, str(fixture["fake_loopx"])))
    report = Dispatcher(config, should_run=should_run, clock=Clock()).run_once()
    assert report["launched"] == []
    assert {"goal_id": GOAL_ID, "agent_id": "acc", "reason": "review_blocked_gate_open",
            "todo_id": todo_id} in report["skipped"]
