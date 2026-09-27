"""Pilot v1 gap N10: a released plan todo does not keep a stale, untouched branch.

A todo branch cut before the todo's dependencies merged still points at the old
merge target. On release it is fast-forwarded when it has no commits of its
own; a branch with developer commits, or a dirty worktree, is never rewritten.
"""
from __future__ import annotations

from pathlib import Path

from loopx.plan_cards import resume_ready_plan_todos
from loopx.todo_acceptance import accept_goal_todo
from loopx.workspace import git_workspace
from loopx.workspace.todo_branch_refresh import refresh_untouched_todo_branches
from tests.control_plane.test_plan_dependency_release import (
    ACC, GOAL, _apply, _deliver, _finish_notes, _fixture, _status,
)
from tests.dispatch.dispatch_fixtures import git

TASK_BRANCH = f"loopx-task/{GOAL}"


def _tip(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", ref)


def _early_prepare(goal: dict, runtime: Path, todo_id: str) -> dict[str, Path]:
    """The early prepare the pilot hit: the todo's branches cut while it is still deferred."""

    prepared = git_workspace.prepare(goal, todo_id, None, runtime)
    assert prepared["ok"], prepared
    return {name: Path(path) for name, path in prepared["paths"].items()}


def _release_integrate(registry: Path, runtime: Path, goal: dict, ids: dict[str, str]) -> dict:
    _finish_notes(registry, ids["notes"])
    _deliver(registry, runtime, goal, ids["front"], {"web/ui.txt": "frontend\n"})
    return accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], agent_id=ACC)


def test_release_fast_forwards_an_untouched_todo_branch(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch)
    ids = _apply(registry, runtime)
    paths = _early_prepare(goal, runtime, ids["integrate"])
    web = Path(goal["repos"][1]["path"])
    branch = git_workspace.todo_branch(GOAL, ids["integrate"])
    stale = _tip(web, branch)

    accepted = _release_integrate(registry, runtime, goal, ids)
    assert ids["integrate"] in accepted.get("resumed_todo_ids", [])
    assert _status(registry, ids["integrate"]) == "open"
    # The front merge moved the web task branch; the untouched todo branch followed it.
    assert _tip(web, TASK_BRANCH) != stale
    assert _tip(web, branch) == _tip(web, TASK_BRANCH)
    assert (paths["web"] / "ui.txt").read_text(encoding="utf-8") == "frontend\n"
    assert _tip(paths["web"], "HEAD") == _tip(web, TASK_BRANCH)


def test_release_never_rewrites_a_branch_with_developer_commits(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch)
    ids = _apply(registry, runtime)
    paths = _early_prepare(goal, runtime, ids["integrate"])
    web = Path(goal["repos"][1]["path"])
    branch = git_workspace.todo_branch(GOAL, ids["integrate"])
    (paths["web"] / "wip.txt").write_text("developer work\n", encoding="utf-8")
    git(paths["web"], "add", ".")
    git(paths["web"], "commit", "-qm", "developer work")
    own = _tip(web, branch)

    _release_integrate(registry, runtime, goal, ids)
    assert _status(registry, ids["integrate"]) == "open"
    assert _tip(web, branch) == own

    refresh = refresh_untouched_todo_branches(goal, ids["integrate"], ["web"], runtime)
    assert refresh["repos"][0]["action"] == "skipped"
    assert refresh["repos"][0]["reason"] == "branch_has_own_commits"


def test_a_dirty_worktree_is_left_alone_and_the_refresh_is_reported(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch)
    ids = _apply(registry, runtime)
    paths = _early_prepare(goal, runtime, ids["integrate"])
    web = Path(goal["repos"][1]["path"])
    branch = git_workspace.todo_branch(GOAL, ids["integrate"])
    stale = _tip(web, branch)
    (paths["web"] / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
    # Finish the dependencies without the accept path's own resume.
    _finish_notes(registry, ids["notes"])
    _deliver(registry, runtime, goal, ids["front"], {"web/ui.txt": "frontend\n"})
    monkeypatch.setattr("loopx.plan_cards.resume_ready_plan_todos", lambda **_: [])
    accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], agent_id=ACC)
    monkeypatch.undo()

    refreshes: list[dict] = []
    assert resume_ready_plan_todos(registry_path=registry, goal_id=GOAL, runtime_root=runtime,
                                   branch_refreshes=refreshes) == [ids["integrate"]]
    assert _tip(web, branch) == stale and refreshes == []
    report = refresh_untouched_todo_branches(goal, ids["integrate"], ["web"], runtime, dry_run=True)
    assert (report["repos"][0]["action"], report["repos"][0]["reason"]) == ("skipped", "worktree_dirty")

    # Once the worktree is clean, the same refresh fast-forwards and reports it.
    (paths["web"] / "scratch.txt").unlink()
    moved = refresh_untouched_todo_branches(goal, ids["integrate"], None, runtime)
    assert moved["refreshed"] == ["web"]
    assert {repo["name"]: repo["action"] for repo in moved["repos"]} == {"api": "up_to_date", "web": "fast_forwarded"}
    assert _tip(web, branch) == _tip(web, TASK_BRANCH)
