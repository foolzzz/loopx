"""A deferred todo offered by should-run is never launched (E2E pilot v1).

Upstream should-run can select a deferred plan todo for a developer once its
single ``resume_when`` dependency is done (``successor_replan_required``),
while another decision-37 dependency still waits for accept+merge. A role_v1
todo-lane Turn is pinned, and run-once refuses a pinned deferred todo without
calling the host, so launching it only grew the todo's backoff. The pass
fills the slot with an executable todo instead, or skips with the wait.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from loopx.dispatch import DispatchConfig, Dispatcher, policy
from loopx.plan_dependencies import write_dependency_wait_snapshot
from loopx.todos import add_goal_todo
from tests.dispatch.dispatch_fixtures import GOAL_ID, git, git_env, make_repo, write_fixture
from tests.dispatch.test_acceptor_review_dispatch import Clock

WAIT = "waiting for dependency todo_backend (status in_review)"


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    return write_fixture(
        tmp_path,
        agents={"orch": {"role": "orchestrator"}, "dev": {"role": "developer"}, "acc": {"role": "acceptor"}},
    )


def _todo(fixture: dict[str, Any], text: str) -> str:
    return str(add_goal_todo(
        registry_path=fixture["registry"], goal_id=GOAL_ID, runtime_root_arg=str(fixture["runtime"]),
        role="agent", text=text, task_class="advancement_task", claimed_by="dev",
    )["todo_id"])


def _dispatcher(fixture: dict[str, Any], dev_payload: dict[str, Any]) -> Dispatcher:
    def should_run(goal_id: str, agent_id: str) -> dict[str, Any]:
        return dev_payload if agent_id == "dev" else {"should_run": False}

    config = DispatchConfig(registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_ids=[GOAL_ID],
                            no_global_sync=True, environ=fixture["environ"],
                            loopx_argv=(sys.executable, str(fixture["fake_loopx"])))
    return Dispatcher(config, should_run=should_run, clock=Clock())


def _deferred_offer(todo_id: str, *, executable: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "should_run": True,
        "effective_action": "successor_replan_required",
        "selected_todo": {"todo_id": todo_id, "role": "agent", "status": "deferred"},
        "agent_todo_summary": {"first_executable_items": executable or []},
    }


def test_decide_turn_carries_the_selected_status() -> None:
    decision = policy.decide_turn(_deferred_offer("todo_x"), role=policy.ROLE_DEVELOPER, state_changed=False)
    assert decision["launch"] is True and decision["todo_status"] == "deferred"


def test_a_deferred_offer_is_skipped_with_its_dependency_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    integration = _todo(fixture, "Integrate api and web")
    write_dependency_wait_snapshot(fixture["runtime"], GOAL_ID, {integration: [WAIT]})

    dispatcher = _dispatcher(fixture, _deferred_offer(integration))
    report = dispatcher.run_once()

    assert report["launched"] == [], report
    assert {"goal_id": GOAL_ID, "agent_id": "dev", "reason": policy.SELECTED_TODO_DEFERRED_REASON,
            "todo_id": integration, "dependency_wait": WAIT} in report["skipped"], report
    # No refused launch, so no backoff accrues on the todo.
    assert not (dispatcher.state.get("todo_cooldowns") or {}), dispatcher.state.get("todo_cooldowns")


def test_a_deferred_offer_without_a_recorded_wait_is_still_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    integration = _todo(fixture, "Integrate api and web")
    report = _dispatcher(fixture, _deferred_offer(integration)).run_once()
    assert report["launched"] == [], report
    assert {"goal_id": GOAL_ID, "agent_id": "dev", "reason": policy.SELECTED_TODO_DEFERRED_REASON,
            "todo_id": integration} in report["skipped"], report


def test_a_free_slot_takes_an_executable_todo_instead_of_the_deferred_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    integration = _todo(fixture, "Integrate api and web")
    docs = _todo(fixture, "Write the api notes")
    offer = _deferred_offer(integration, executable=[
        {"todo_id": integration, "status": "deferred", "role": "agent"},
        {"todo_id": docs, "status": "open", "role": "agent"},
    ])
    report = _dispatcher(fixture, offer).run_once()
    launched = [(item["agent_id"], item["todo_id"], item["reason"]) for item in report["launched"]]
    assert launched == [("dev", docs, "alternate_todo")], report


def test_no_workspace_is_cut_for_a_deferred_offer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # E2E pilot v1: the refused launch had already prepared the todo's
    # worktree, so its branch was cut from a task branch that still lacked
    # the backend merge, and the accept merge later conflicted.
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    api = make_repo(tmp_path, "api")
    fixture = write_fixture(
        tmp_path,
        agents={"orch": {"role": "orchestrator"}, "dev": {"role": "developer"}, "acc": {"role": "acceptor"}},
        repos={"api": api},
    )
    integration = str(add_goal_todo(
        registry_path=fixture["registry"], goal_id=GOAL_ID, runtime_root_arg=str(fixture["runtime"]),
        role="agent", text="Integrate api", task_class="advancement_task", claimed_by="dev",
        role_contract={"task_repositories": ["api"]},
    )["todo_id"])
    report = _dispatcher(fixture, _deferred_offer(integration)).run_once()
    assert report["launched"] == [], report
    assert not (fixture["runtime"] / "goals" / GOAL_ID / "workspaces" / integration).exists()
    assert git(api, "branch", "--list", f"loopx/{GOAL_ID}/{integration}").strip() == ""
