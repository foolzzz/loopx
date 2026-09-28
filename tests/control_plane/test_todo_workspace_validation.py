"""E2E pilot regression: S5 task_repositories Todos validate in their per-Todo workspace.

Before the fix the declared validation ran in the Goal repository, which for a
central progress home holds none of the code, so every delivery failed.
"""

from __future__ import annotations

import json
import subprocess
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
        goal["coordination"] = {"agent_model": "role_v1", "registered_agents": ["orch", "dev", "acc"],
                                "agent_roles": {"orch": "orchestrator", "dev": "developer", "acc": "acceptor"}}
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"schema_version": 1, "common_runtime_root": str(runtime), "goals": [goal]}),
                        encoding="utf-8")
    return registry, runtime, goal


def _add(registry: Path, repos: list[str], argv: list[str], text: str = "Build it", **extra) -> str:
    added = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text=text,
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


def test_without_a_prepared_workspace_validation_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """Review fix: no fallback to the Goal repo, which lacks the Todo's commits."""

    registry, _runtime, _goal = _fixture(tmp_path, monkeypatch)
    todo_id = _add(registry, ["api"], ["test", "-f", "ACTIVE_GOAL_STATE.md"])
    blocked = _complete(registry, todo_id)
    assert blocked.get("validation_blocked_completion") is True, blocked
    assert blocked["validation"]["status"] == "workspace_unverified"
    assert _status(registry, todo_id) == "open"


def test_a_worktree_off_the_todo_branch_fails_closed(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch)
    todo_id = _add(registry, ["api"], ["true"])
    worktree = Path(git_workspace.prepare(goal, todo_id, None, runtime)["paths"]["api"])
    git(worktree, "checkout", "-q", "--detach")
    blocked = _complete(registry, todo_id)
    assert blocked["validation"]["status"] == "workspace_unverified"
    assert _status(registry, todo_id) == "open"


def test_validation_uses_the_callers_runtime_root(tmp_path: Path, monkeypatch) -> None:
    """Review fix: the dispatcher's --runtime-root is where it prepared the workspace."""

    registry, _runtime, goal = _fixture(tmp_path, monkeypatch)
    other = tmp_path / "dispatch-runtime"
    root_arg = {"runtime_root_arg": str(other)}
    todo_id = _add(registry, ["api"], ["test", "-f", "feature.txt"], **root_arg)
    worktree = Path(git_workspace.prepare(goal, todo_id, None, other)["paths"]["api"])
    (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", "feature")
    assert _complete(registry, todo_id, **root_arg).get("ok") is not False
    assert next(row["status"] for row in list_goal_todos(registry_path=registry, goal_id=GOAL, **root_arg)["todos"]
                if row["todo_id"] == todo_id) == "done"


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


def _deliver(registry: Path, runtime: Path, goal: dict, repos: list[str], files: dict[str, str]) -> str:
    todo_id = _add(registry, repos, ["true"], text=f"Build {sorted(files)}", claimed_by="dev")
    paths = git_workspace.prepare(goal, todo_id, None, runtime)["paths"]
    for name, content in files.items():
        repo, rel = name.split("/", 1)
        (Path(paths[repo]) / rel).write_text(content, encoding="utf-8")
    for repo in {name.split("/", 1)[0] for name in files}:
        git(Path(paths[repo]), "add", ".")
        git(Path(paths[repo]), "commit", "-qm", f"work on {todo_id}")
    assert _complete(registry, todo_id, agent_id="dev").get("in_review") is True
    return todo_id


def test_accept_merges_every_repo_of_the_todo_into_the_task_branch(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch, roles=True)
    todo_id = _deliver(registry, runtime, goal, ["api", "web"], {"api/a.txt": "a\n", "web/w.txt": "w\n"})
    accepted = accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, agent_id="acc")
    assert accepted["merge"]["ok"] is True
    assert sorted(item["name"] for item in accepted["merge"]["merged"]) == ["api", "web"]
    for repo, name in (("api", "a.txt"), ("web", "w.txt")):
        repo_dir = tmp_path / "repos" / repo
        assert git(repo_dir, "show", f"loopx-task/{GOAL}:{name}") == name[0]
        # The default branch is untouched and nothing was pushed anywhere.
        assert git(repo_dir, "rev-parse", "main") == git(repo_dir, "rev-parse", "main~0")
        assert git(repo_dir, "remote") == ""
    assert _status(registry, todo_id) == "done"


def test_a_merge_conflict_returns_the_todo_to_its_developer(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch, roles=True)
    first = _deliver(registry, runtime, goal, ["api"], {"api/shared.txt": "first\n"})
    second = _deliver(registry, runtime, goal, ["api"], {"api/shared.txt": "second\n", "api/other.txt": "o\n"})
    assert accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=first, agent_id="acc")["merge"]["ok"]
    blocked = accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=second, agent_id="acc")
    assert blocked["acceptance"]["transition"] == "merge_blocked"
    assert blocked["merge"]["error_code"] == "merge_blocked"
    row = next(r for r in list_goal_todos(registry_path=registry, goal_id=GOAL)["todos"] if r["todo_id"] == second)
    assert row["status"] == "open" and row["claimed_by"] == "dev"
    assert "merge_conflict (shared.txt)" in row["review_feedback"]
    assert not row.get("reject_count")
    api = tmp_path / "repos" / "api"
    assert git(api, "show", f"loopx-task/{GOAL}:shared.txt") == "first"


def _plan_todos(registry: Path, runtime: Path) -> dict[str, str]:
    """Apply a plan: ``first`` and ``second`` edit api; ``after`` depends on ``second``."""

    from loopx.plan_cards import propose_plan

    plan = {"title": "Race", "summary": "Two api todos, one dependent.", "todos": [
        {"key": "first", "text": "First api change", "bound_agent": "dev", "task_repositories": ["api"]},
        {"key": "second", "text": "Second api change", "bound_agent": "dev", "task_repositories": ["api"]},
        {"key": "after", "text": "Builds on the second", "bound_agent": "dev", "depends_on": ["second"],
         "task_repositories": ["api"]},
    ]}
    proposed = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id="orch", plan=plan)
    done = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=proposed["plan"]["gate_todo_id"],
                              role="user", decision_outcome="approve", note="Go", no_followup=True, agent_id="orch")
    return dict(done["plan_card"]["todo_id_map"])


def _commit_in_workspace(runtime: Path, goal: dict, todo_id: str, files: dict[str, str]) -> Path:
    worktree = Path(git_workspace.prepare(goal, todo_id, None, runtime)["paths"]["api"])
    for rel, content in files.items():
        (worktree / rel).write_text(content, encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", f"work on {todo_id}")
    return worktree


def test_back_to_back_accepts_never_complete_a_todo_whose_merge_failed(tmp_path: Path, monkeypatch) -> None:
    """Review fix: accept used to complete the todo before its real merge.

    The second accept's merge check passes, then the first todo's accept lands
    on the task branch before the second's real merge. The second todo must
    not become done, its plan dependent must not resume, and a redelivery
    must still complete it.
    """

    registry, runtime, goal = _fixture(tmp_path, monkeypatch, roles=True)
    ids = _plan_todos(registry, runtime)
    _commit_in_workspace(runtime, goal, ids["first"], {"shared.txt": "first\n"})
    second_tree = _commit_in_workspace(runtime, goal, ids["second"], {"shared.txt": "second\n"})
    for key in ("first", "second"):
        assert _complete(registry, ids[key], agent_id="dev").get("in_review") is True

    real_merge = git_workspace.merge
    raced: list[dict] = []

    def racing_merge(goal_arg, todo_id, repos, runtime_root, *, dry_run=False, **pins):
        if todo_id == ids["second"] and not dry_run and not raced:
            # The first todo's accept lands between the check and the merge.
            raced.append(accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=ids["first"],
                                          agent_id="acc"))
        return real_merge(goal_arg, todo_id, repos, runtime_root, dry_run=dry_run, **pins)

    monkeypatch.setattr(git_workspace, "merge", racing_merge)
    blocked = accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=ids["second"], agent_id="acc")
    assert raced and raced[0]["merge"]["ok"] is True and _status(registry, ids["first"]) == "done"
    assert blocked["acceptance"]["transition"] == "merge_blocked", blocked
    assert blocked["merge"]["ok"] is False
    assert _status(registry, ids["second"]) == "open"
    assert _status(registry, ids["after"]) == "deferred"
    assert "resumed_todo_ids" not in blocked
    api = tmp_path / "repos" / "api"
    assert git(api, "show", f"loopx-task/{GOAL}:shared.txt") == "first"

    # Recoverable: the developer resolves on the todo branch and delivers again.
    subprocess.run(["git", "merge", "-q", f"loopx-task/{GOAL}"], cwd=second_tree, capture_output=True, check=False)
    (second_tree / "shared.txt").write_text("first\nsecond\n", encoding="utf-8")
    git(second_tree, "add", ".")
    git(second_tree, "commit", "-qm", "resolve")
    assert _complete(registry, ids["second"], agent_id="dev").get("in_review") is True
    accepted = accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=ids["second"], agent_id="acc")
    assert accepted["acceptance"]["transition"] == "accepted" and accepted["merge"]["ok"] is True
    assert _status(registry, ids["second"]) == "done"
    assert accepted["resumed_todo_ids"] == [ids["after"]]
    assert git(api, "show", f"loopx-task/{GOAL}:shared.txt") == "first\nsecond"


def test_a_completion_refused_after_the_merge_is_retry_safe(tmp_path: Path, monkeypatch) -> None:
    """The merge lands, then the re-run validation refuses completion.

    The todo stays in_review with nothing resumed; a retry re-merges as a
    no-op and completes.
    """

    registry, runtime, goal = _fixture(tmp_path, monkeypatch, roles=True)
    todo_id = _add(registry, ["api"], ["test", "!", "-f", str(tmp_path / "veto")], claimed_by="dev")
    _commit_in_workspace(runtime, goal, todo_id, {"a.txt": "a\n"})
    assert _complete(registry, todo_id, agent_id="dev").get("in_review") is True
    (tmp_path / "veto").write_text("x", encoding="utf-8")
    refused = accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, agent_id="acc")
    assert refused.get("validation_blocked_completion") is True, refused
    assert refused["acceptance"]["transition"] == "completion_blocked"
    assert refused["merge"]["ok"] is True and len(refused["merge"]["merged"]) == 1
    assert _status(registry, todo_id) == "in_review"
    api = tmp_path / "repos" / "api"
    head = git(api, "rev-parse", f"loopx-task/{GOAL}")
    (tmp_path / "veto").unlink()
    retried = accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, agent_id="acc")
    assert retried["acceptance"]["transition"] == "accepted"
    assert retried["merge"] == {**retried["merge"], "ok": True, "merged": []}
    assert git(api, "rev-parse", f"loopx-task/{GOAL}") == head
    assert _status(registry, todo_id) == "done"


def test_accept_merges_from_the_todo_branch_after_its_worktree_is_gone(tmp_path: Path, monkeypatch) -> None:
    """Review fix: merge eligibility keys on the todo branch, not on directories."""

    registry, runtime, goal = _fixture(tmp_path, monkeypatch, roles=True)
    added = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="No validation",
                          task_class="advancement_task", claimed_by="dev",
                          role_contract={"task_repositories": ["api"]})
    todo_id = str(added["todo_id"])
    worktree = _commit_in_workspace(runtime, goal, todo_id, {"gone.txt": "g\n"})
    assert _complete(registry, todo_id, agent_id="dev").get("in_review") is True
    api = tmp_path / "repos" / "api"
    git(api, "worktree", "remove", "--force", str(worktree))
    assert not worktree.exists()
    accepted = accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, agent_id="acc")
    assert accepted["acceptance"]["transition"] == "accepted", accepted
    assert accepted["merge"]["ok"] is True and len(accepted["merge"]["merged"]) == 1
    assert git(api, "show", f"loopx-task/{GOAL}:gone.txt") == "g"
    assert _status(registry, todo_id) == "done"


def test_accept_blocks_when_a_repo_lacks_the_todo_branch(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch, roles=True)
    added = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Two repos",
                          task_class="advancement_task", claimed_by="dev",
                          role_contract={"task_repositories": ["api", "web"]})
    todo_id = str(added["todo_id"])
    git_workspace.prepare(goal, todo_id, None, runtime)
    assert _complete(registry, todo_id, agent_id="dev").get("in_review") is True
    web = tmp_path / "repos" / "web"
    git(web, "worktree", "remove", "--force", str(runtime / "goals" / GOAL / "workspaces" / todo_id / "web"))
    git(web, "branch", "-D", f"loopx/{GOAL}/{todo_id}")
    blocked = accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, agent_id="acc")
    assert blocked["acceptance"]["transition"] == "merge_blocked", blocked
    assert "web: todo_branch_missing" in blocked["acceptance"]["review_feedback"]
    assert _status(registry, todo_id) == "open"
