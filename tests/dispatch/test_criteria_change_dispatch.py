"""Decision 40: the dispatcher does not launch the acceptor on a todo whose
acceptance-criteria change awaits the user on a plan card, and launches it
once the card is decided."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from loopx.dispatch import DispatchConfig, Dispatcher
from loopx.plan_cards import propose_plan
from loopx.todos import add_goal_todo, complete_goal_todo, list_goal_todos
from loopx.workspace import git_workspace
from tests.dispatch.dispatch_fixtures import GOAL_ID, git, git_env, make_repo, write_fixture
from tests.dispatch.test_acceptor_review_dispatch import Clock, _dispatcher

NEW_CRITERIA = "the api fixture exposes feature.txt"


def _delivered_with_orchestrator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], str]:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    api = make_repo(tmp_path, "api")
    git(api, "remote", "add", "origin", "https://example.test/fixture/api.git")
    fixture = write_fixture(
        tmp_path,
        agents={"orch": {"role": "orchestrator"}, "dev": {"role": "developer"}, "acc": {"role": "acceptor"}},
        repos={"api": api},
    )
    fixture["api"] = api
    kwargs = {"registry_path": fixture["registry"], "goal_id": GOAL_ID, "runtime_root_arg": str(fixture["runtime"])}
    todo_id = str(add_goal_todo(**kwargs, role="agent", text="Build the api fixture", priority="P0",
                                task_class="advancement_task", claimed_by="dev",
                                validation_command_json=json.dumps(["true"]),
                                role_contract={"task_repositories": ["api"]})["todo_id"])
    goal = json.loads(fixture["registry"].read_text())["goals"][0]
    worktree = Path(git_workspace.prepare(goal, todo_id, None, fixture["runtime"])["paths"]["api"])
    (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", "feature")
    delivered = complete_goal_todo(**kwargs, todo_id=todo_id, role="agent", agent_id="dev", evidence="built")
    assert delivered.get("in_review") is True, delivered
    return fixture, todo_id


def _propose(fixture: dict[str, Any], todo_id: str) -> dict[str, Any]:  # one criteria change
    return propose_plan(
        registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID, agent_id="orch",
        runtime_root_arg=str(fixture["runtime"]),
        plan={"title": "Name the feature file", "criteria_changes": [
            {"todo_id": todo_id, "new": NEW_CRITERIA, "reason": "the user named the file"}]},
    )["plan"]


def _todo(fixture: dict[str, Any], todo_id: str) -> dict[str, Any]:
    listed = list_goal_todos(registry_path=fixture["registry"], goal_id=GOAL_ID,
                             runtime_root_arg=str(fixture["runtime"]))
    return next(row for row in listed["todos"] if row["todo_id"] == todo_id)


def test_the_acceptor_waits_for_the_card_and_runs_after_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, todo_id = _delivered_with_orchestrator(tmp_path, monkeypatch)
    plan = _propose(fixture, todo_id)

    held = _dispatcher(fixture, FAKE_CLAUDE_RESULT_KIND="validated_completion").run_once()
    assert held["launched"] == [], held
    skip = next(item for item in held["skipped"] if item["agent_id"] == "acc")
    assert skip.get("criteria_change_pending_todo_ids") == [todo_id] or skip["reason"] == "criteria_change_pending", skip

    complete_goal_todo(registry_path=fixture["registry"], goal_id=GOAL_ID, todo_id=plan["gate_todo_id"],
                       role="user", decision_outcome="approve", note="ok", no_followup=True, agent_id="orch",
                       runtime_root_arg=str(fixture["runtime"]))
    assert _todo(fixture, todo_id)["acceptance_criteria"] == NEW_CRITERIA
    after = _dispatcher(fixture, FAKE_CLAUDE_RESULT_KIND="validated_completion",
                        FAKE_CLAUDE_SUMMARY="Every criterion holds.").run_once()
    assert [(item["agent_id"], item["todo_id"]) for item in after["launched"]] == [("acc", todo_id)], after


def test_the_dispatcher_skips_an_offered_todo_with_a_pending_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, todo_id = _delivered_with_orchestrator(tmp_path, monkeypatch)
    _propose(fixture, todo_id)

    def should_run(goal_id: str, agent_id: str) -> dict[str, Any]:
        # Even if should-run offered the todo, the dispatcher must not launch the acceptor on it.
        if agent_id != "acc":
            return {"should_run": False}
        return {"should_run": True, "effective_action": "normal_run",
                "selected_todo": {"todo_id": todo_id, "role": "agent"}}

    config = DispatchConfig(registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_ids=[GOAL_ID],
                            no_global_sync=True, environ=fixture["environ"],
                            loopx_argv=(sys.executable, str(fixture["fake_loopx"])))
    report = Dispatcher(config, should_run=should_run, clock=Clock()).run_once()
    assert report["launched"] == []
    assert {"goal_id": GOAL_ID, "agent_id": "acc", "reason": "criteria_change_pending",
            "todo_id": todo_id} in report["skipped"], report


def test_a_pending_card_on_another_todo_does_not_hold_the_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, todo_id = _delivered_with_orchestrator(tmp_path, monkeypatch)
    other = str(add_goal_todo(registry_path=fixture["registry"], goal_id=GOAL_ID,
                              runtime_root_arg=str(fixture["runtime"]), role="agent", text="Write the api notes",
                              task_class="advancement_task", claimed_by="dev",
                              role_contract={"requires_acceptance": False})["todo_id"])
    _propose(fixture, other)
    report = _dispatcher(fixture, FAKE_CLAUDE_RESULT_KIND="validated_completion",
                         FAKE_CLAUDE_SUMMARY="Every criterion holds.").run_once()
    launched = {(item["agent_id"], item["todo_id"]) for item in report["launched"]}
    assert ("acc", todo_id) in launched, report
