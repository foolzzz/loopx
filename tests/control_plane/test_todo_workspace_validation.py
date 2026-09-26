"""E2E pilot regression: S5 task_repositories Todos validate in their per-Todo workspace.

Before the fix the declared validation ran in the Goal repository, which for a
central progress home holds none of the code, so every delivery failed.
"""

from __future__ import annotations

import json
from pathlib import Path

from loopx.todo_acceptance import accept_goal_todo
from loopx.todos import add_goal_todo, complete_goal_todo, list_goal_todos
from loopx.workspace import git_workspace
from tests.dispatch.dispatch_fixtures import git, git_env, make_repo

GOAL = "ws-validation"


def _fixture(tmp_path: Path, monkeypatch, *, roles: bool = False) -> tuple[Path, Path, dict]:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "progress"
    home.mkdir()
    state = home / "ACTIVE_GOAL_STATE.md"
    state.write_text("# Goal\n\n## User Todo\n\n## Agent Todo\n\n## Completed Work Archive\n", encoding="utf-8")
    api, web = make_repo(tmp_path, "api"), make_repo(tmp_path, "web")
    runtime = tmp_path / "runtime"
    goal = {
        "id": GOAL, "status": "active", "repo": str(home), "state_file": state.name,
        "repos": [{"name": "api", "path": str(api), "default_branch": "main", "merge_target": "task_branch"},
                  {"name": "web", "path": str(web), "default_branch": "main", "merge_target": "task_branch"}],
    }
    if roles:
        goal["coordination"] = {"agent_model": "role_v1", "registered_agents": ["dev", "acc"],
                                "agent_roles": {"dev": "developer", "acc": "acceptor"}}
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"schema_version": 1, "common_runtime_root": str(runtime), "goals": [goal]}),
                        encoding="utf-8")
    return registry, runtime, goal


def _add(registry: Path, repos: list[str], argv: list[str], **extra) -> str:
    added = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Build it",
                          task_class="advancement_task", validation_command_json=json.dumps(argv),
                          role_contract={"task_repositories": repos}, **extra)
    return str(added["todo_id"])


def _complete(registry: Path, todo_id: str, **extra) -> dict:
    return complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, role="agent",
                              evidence="built", **extra)


def _status(registry: Path, todo_id: str) -> str:
    return next(row["status"] for row in list_goal_todos(registry_path=registry, goal_id=GOAL)["todos"]
                if row["todo_id"] == todo_id)


def test_single_repo_todo_validates_in_its_worktree(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch)
    todo_id = _add(registry, ["api"], ["test", "-f", "feature.txt"])
    prepared = git_workspace.prepare(goal, todo_id, None, runtime)
    worktree = Path(prepared["paths"]["api"])
    (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
    # Uncommitted work is refused: the merge only carries committed work.
    blocked = _complete(registry, todo_id)
    assert blocked.get("validation_blocked_completion") is True
    assert _status(registry, todo_id) == "open"
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", "feature")
    assert _complete(registry, todo_id).get("ok") is not False
    assert _status(registry, todo_id) == "done"


def test_multi_repo_todo_validates_in_the_workspace_root(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch)
    todo_id = _add(registry, ["api", "web"], ["test", "-f", "web/README.md"])
    git_workspace.prepare(goal, todo_id, None, runtime)
    assert _complete(registry, todo_id).get("ok") is not False
    assert _status(registry, todo_id) == "done"


def test_without_a_prepared_workspace_validation_stays_in_the_goal_repo(tmp_path: Path, monkeypatch) -> None:
    registry, _runtime, _goal = _fixture(tmp_path, monkeypatch)
    todo_id = _add(registry, ["api"], ["test", "-f", "ACTIVE_GOAL_STATE.md"])
    assert _complete(registry, todo_id).get("ok") is not False
    assert _status(registry, todo_id) == "done"


def test_role_v1_delivery_and_accept_validate_in_the_todo_worktree(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch, roles=True)
    todo_id = _add(registry, ["api"], ["test", "-f", "feature.txt"], claimed_by="dev")
    worktree = Path(git_workspace.prepare(goal, todo_id, None, runtime)["paths"]["api"])
    (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", "feature")
    delivered = _complete(registry, todo_id, agent_id="dev")
    assert delivered.get("in_review") is True, delivered
    assert _status(registry, todo_id) == "in_review"
    accepted = accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, agent_id="acc",
                                note="looks right")
    assert accepted.get("ok") is True, accepted
    assert _status(registry, todo_id) == "done"
