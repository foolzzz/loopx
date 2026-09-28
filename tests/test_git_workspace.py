from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from loopx.cli import main
from loopx.workspace import git_workspace

GOAL = "wsgoal"
TODO = "todo-1"
BRANCH = f"loopx/{GOAL}/{TODO}"


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path, monkeypatch):
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(key, value)


def git(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(root: Path, name: str) -> Path:
    repo = root / "repos" / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "shared.txt").write_text(f"{name} base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    return repo


def commit_file(cwd: Path, rel: str, content: str, message: str = "change") -> str:
    (cwd / rel).write_text(content)
    git(cwd, "add", rel)
    git(cwd, "commit", "-qm", message)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def env(tmp_path):
    repos = {name: make_repo(tmp_path, name) for name in ("api", "web", "docs")}
    goal = {
        "id": GOAL,
        "repos": [
            {"name": name, "path": str(path), "default_branch": "main", "merge_target": "main"}
            for name, path in repos.items()
        ],
    }
    return {"repos": repos, "goal": goal, "runtime": tmp_path / "runtime", "tmp": tmp_path}


# --- prepare -----------------------------------------------------------------


def test_prepare_shares_branch_and_is_idempotent(env):
    dry = git_workspace.prepare(env["goal"], TODO, None, env["runtime"], dry_run=True)
    assert dry["ok"] and {r["action"] for r in dry["repos"]} == {"would_create"}
    assert not (env["runtime"] / "goals" / GOAL / "workspaces" / TODO).exists()

    first = git_workspace.prepare(env["goal"], TODO, None, env["runtime"])
    assert first["ok"], first
    root = env["runtime"] / "goals" / GOAL / "workspaces" / TODO
    for name, repo in env["repos"].items():
        path = root / name
        assert first["paths"][name] == str(path)
        assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == BRANCH
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert {r["action"] for r in first["repos"]} == {"create"}

    commit_file(root / "api", "a.txt", "work\n")
    second = git_workspace.prepare(env["goal"], TODO, None, env["runtime"])
    assert second["ok"] and {r["action"] for r in second["repos"]} == {"reused"}
    assert (root / "api" / "a.txt").exists()

    subset = git_workspace.prepare(env["goal"], "todo-2", ["web"], env["runtime"])
    assert [r["name"] for r in subset["repos"]] == ["web"]
    unknown = git_workspace.prepare(env["goal"], "todo-2", ["nope"], env["runtime"])
    assert unknown["ok"] is False and unknown["error_code"] == "unknown_repo"


def test_prepare_reports_missing_repo_and_occupied_path(env):
    goal = dict(env["goal"])
    goal["repos"] = goal["repos"] + [{"name": "gone", "path": str(env["tmp"] / "missing")}]
    occupied = env["runtime"] / "goals" / GOAL / "workspaces" / TODO / "web"
    occupied.mkdir(parents=True)
    (occupied / "keep.txt").write_text("user data")
    result = git_workspace.prepare(goal, TODO, None, env["runtime"])
    by_name = {r["name"]: r for r in result["repos"]}
    assert result["ok"] is False
    assert by_name["gone"]["error_code"] == "repo_missing"
    assert by_name["web"]["error_code"] == "path_occupied"
    assert by_name["api"]["ok"] is True
    assert (occupied / "keep.txt").read_text() == "user data"


def test_invalid_ids_rejected(env):
    result = git_workspace.prepare(env["goal"], "../escape", None, env["runtime"])
    assert result["ok"] is False and result["error_code"] == "invalid_id"


# --- merge -------------------------------------------------------------------


def _prepared(env, todo=TODO):
    result = git_workspace.prepare(env["goal"], todo, None, env["runtime"])
    assert result["ok"], result
    return {name: Path(path) for name, path in result["paths"].items()}


def test_clean_atomic_merge_across_repos(env):
    paths = _prepared(env)
    commit_file(paths["api"], "api.txt", "endpoint\n")
    commit_file(paths["web"], "web.txt", "client\n")
    # web's main checkout sits on another branch, so its main is a bare ref move.
    git(env["repos"]["web"], "checkout", "-q", "-b", "side")
    old_heads = {name: git(repo, "rev-parse", "main") for name, repo in env["repos"].items()}

    status = git_workspace.status(env["goal"], TODO, None, env["runtime"])
    assert status["mergeable"] is True
    by_name = {r["name"]: r for r in status["repos"]}
    assert by_name["api"]["ahead"] == 1 and by_name["api"]["behind"] == 0
    assert by_name["docs"]["merge_check"]["state"] == "up_to_date"

    dry = git_workspace.merge(env["goal"], TODO, None, env["runtime"], dry_run=True)
    assert dry["ok"] and sorted(dry["would_merge"]) == ["api", "web"]
    assert git(env["repos"]["api"], "rev-parse", "main") == old_heads["api"]

    result = git_workspace.merge(env["goal"], TODO, None, env["runtime"])
    assert result["ok"], result
    modes = {item["name"]: item["mode"] for item in result["merged"]}
    assert modes == {"api": "worktree_ff", "web": "ref"}
    for name in ("api", "web"):
        repo = env["repos"][name]
        parents = git(repo, "rev-list", "--parents", "-n1", "main").split()
        assert parents[1] == old_heads[name] and len(parents) == 3  # no-ff merge commit
        message = git(repo, "log", "-1", "--format=%B", "main")
        assert f"LoopX-Goal: {GOAL}" in message and f"LoopX-Todo: {TODO}" in message
    assert (env["repos"]["api"] / "api.txt").read_text() == "endpoint\n"  # checkout updated
    assert git(env["repos"]["api"], "status", "--porcelain") == ""
    assert git(env["repos"]["docs"], "rev-parse", "main") == old_heads["docs"]


def test_conflict_in_one_repo_merges_nothing(env):
    paths = _prepared(env)
    commit_file(paths["api"], "api.txt", "endpoint\n")
    commit_file(paths["web"], "shared.txt", "todo edit\n")
    commit_file(env["repos"]["web"], "shared.txt", "main edit\n")
    old_heads = {name: git(repo, "rev-parse", "main") for name, repo in env["repos"].items()}

    status = git_workspace.status(env["goal"], TODO, ["web"], env["runtime"])
    assert status["repos"][0]["merge_blockers"] == ["merge_conflict"]

    result = git_workspace.merge(env["goal"], TODO, None, env["runtime"])
    assert result["ok"] is False and result["error_code"] == "merge_blocked"
    assert result["merged"] == []
    report = result["conflict_report"]
    assert report["merged_nothing"] is True
    assert [repo["name"] for repo in report["repos"]] == ["web"]
    blocker = report["repos"][0]["blockers"][0]
    assert blocker["error_code"] == "merge_conflict"
    assert blocker["conflicted_paths"] == ["shared.txt"]
    assert any("CONFLICT" in line for line in blocker["messages"])
    for name, repo in env["repos"].items():
        assert git(repo, "rev-parse", "main") == old_heads[name]


def test_blockers_dirty_target_checkout_dirty_and_detached_todo_worktree(env):
    paths = _prepared(env)
    commit_file(paths["api"], "api.txt", "endpoint\n")
    (env["repos"]["api"] / "shared.txt").write_text("uncommitted on main checkout\n")
    (paths["web"] / "wip.txt").write_text("not committed\n")
    git(paths["docs"], "checkout", "-q", "--detach")
    result = git_workspace.merge(env["goal"], TODO, None, env["runtime"])
    assert result["ok"] is False
    codes = {
        repo["name"]: [b["error_code"] for b in repo["blockers"]]
        for repo in result["conflict_report"]["repos"]
    }
    assert codes == {
        "api": ["target_checkout_dirty"],
        "web": ["todo_worktree_dirty"],
        "docs": ["todo_worktree_detached"],
    }


def test_midway_failure_rolls_back_merged_repos(env, monkeypatch):
    paths = _prepared(env)
    for name in ("api", "web", "docs"):
        commit_file(paths[name], f"{name}.txt", "work\n")
    git(env["repos"]["web"], "checkout", "-q", "-b", "side")  # ref mode for web
    old_heads = {name: git(repo, "rev-parse", "main") for name, repo in env["repos"].items()}
    real_apply = git_workspace._apply_repo_merge

    def flaky(plan, message):
        if plan["name"] == "docs":
            raise git_workspace.WorkspaceError("git_failed", "simulated failure")
        return real_apply(plan, message)

    monkeypatch.setattr(git_workspace, "_apply_repo_merge", flaky)
    result = git_workspace.merge(env["goal"], TODO, None, env["runtime"])
    assert result["ok"] is False and result["error_code"] == "merge_apply_failed"
    assert result["failure"]["name"] == "docs"
    assert sorted(item["name"] for item in result["rolled_back"]) == ["api", "web"]
    assert result["rollback_failures"] == []
    for name, repo in env["repos"].items():
        assert git(repo, "rev-parse", "main") == old_heads[name]
    assert git(env["repos"]["api"], "status", "--porcelain") == ""
    assert not (env["repos"]["api"] / "api.txt").exists()

    monkeypatch.setattr(git_workspace, "_apply_repo_merge", real_apply)
    retry = git_workspace.merge(env["goal"], TODO, None, env["runtime"])
    assert retry["ok"] and len(retry["merged"]) == 3


def test_merge_target_task_branch(env):
    goal = dict(env["goal"])
    goal["repos"] = [
        {**repo, "merge_target": "task_branch"} if repo["name"] != "docs" else
        {**repo, "merge_target": "task_branch", "task_branch": "integration"}
        for repo in goal["repos"]
    ]
    paths = git_workspace.prepare(goal, TODO, None, env["runtime"])["paths"]
    api_main = git(env["repos"]["api"], "rev-parse", "main")
    assert git(env["repos"]["api"], "rev-parse", f"loopx-task/{GOAL}") == api_main
    commit_file(Path(paths["api"]), "api.txt", "endpoint\n")
    commit_file(Path(paths["docs"]), "docs.txt", "doc\n")
    result = git_workspace.merge(goal, TODO, None, env["runtime"])
    assert result["ok"], result
    targets = {item["name"]: item["target_branch"] for item in result["merged"]}
    assert targets == {"api": f"loopx-task/{GOAL}", "docs": "integration"}
    assert git(env["repos"]["api"], "rev-parse", "main") == api_main
    assert git(env["repos"]["api"], "show", f"loopx-task/{GOAL}:api.txt") == "endpoint"

    # The next todo branches from the task branch and sees earlier accepted work.
    later = git_workspace.prepare(goal, "todo-2", ["api"], env["runtime"])
    assert later["repos"][0]["base_branch"] == f"loopx-task/{GOAL}"
    assert (Path(later["paths"]["api"]) / "api.txt").exists()


# --- cleanup -----------------------------------------------------------------


def test_cleanup_only_removes_merged_and_never_touches_unrelated(env):
    paths = _prepared(env)
    commit_file(paths["api"], "api.txt", "endpoint\n")
    unrelated = env["tmp"] / "unrelated-wt"
    git(env["repos"]["api"], "worktree", "add", "-q", "-b", "feature/x", str(unrelated))
    other_todo = _prepared(env, "todo-other")

    skipped = git_workspace.cleanup(env["goal"], TODO, ["api"], env["runtime"])
    assert skipped["ok"] is False and skipped["repos"][0]["error_code"] == "not_merged"
    assert paths["api"].is_dir()

    assert git_workspace.merge(env["goal"], TODO, None, env["runtime"])["ok"]
    dry = git_workspace.cleanup(env["goal"], TODO, None, env["runtime"], dry_run=True)
    assert dry["ok"] and {r["action"] for r in dry["repos"]} == {"would_remove"}
    assert paths["api"].is_dir()

    done = git_workspace.cleanup(env["goal"], TODO, None, env["runtime"])
    assert done["ok"], done
    for name, repo in env["repos"].items():
        assert not paths[name].exists()
        assert git(repo, "branch", "--list", BRANCH) == ""
    assert not (env["runtime"] / "goals" / GOAL / "workspaces" / TODO).exists()
    assert unrelated.is_dir() and git(unrelated, "rev-parse", "--abbrev-ref", "HEAD") == "feature/x"
    assert all(path.is_dir() for path in other_todo.values())

    again = git_workspace.cleanup(env["goal"], TODO, None, env["runtime"])
    assert again["ok"] and {r["action"] for r in again["repos"]} == {"nothing_to_clean"}


def test_cleanup_force_dirty_and_foreign_paths(env):
    paths = _prepared(env)
    commit_file(paths["api"], "api.txt", "unmerged\n")
    (paths["web"] / "wip.txt").write_text("dirty\n")
    git(paths["docs"], "checkout", "-q", "-b", "someone-else")
    result = git_workspace.cleanup(env["goal"], TODO, None, env["runtime"])
    codes = {r["name"]: r.get("error_code") for r in result["repos"]}
    assert codes == {"api": "not_merged", "web": "worktree_dirty", "docs": "foreign_worktree"}

    forced = git_workspace.cleanup(env["goal"], TODO, ["api", "web"], env["runtime"], force=True)
    assert forced["ok"], forced
    assert not paths["api"].exists() and not paths["web"].exists()
    assert git(env["repos"]["api"], "branch", "--list", BRANCH) == ""
    assert paths["docs"].is_dir()  # foreign branch in our path is left alone even when forced
    refused = git_workspace.cleanup(env["goal"], TODO, ["docs"], env["runtime"], force=True)
    assert refused["repos"][0]["error_code"] == "foreign_worktree"


def test_cleanup_handles_manually_deleted_worktree_directory(env):
    import shutil

    paths = _prepared(env)
    shutil.rmtree(paths["api"])
    reprepared = git_workspace.prepare(env["goal"], TODO, ["api"], env["runtime"])
    assert reprepared["ok"] and paths["api"].is_dir()
    shutil.rmtree(paths["api"])
    stale_other = env["tmp"] / "stale-other"
    git(env["repos"]["api"], "worktree", "add", "-q", "-b", "stale", str(stale_other))
    shutil.rmtree(stale_other)
    done = git_workspace.cleanup(env["goal"], TODO, ["api"], env["runtime"], force=True)
    assert done["ok"], done
    listing = git(env["repos"]["api"], "worktree", "list", "--porcelain")
    assert str(paths["api"]) not in listing
    assert "stale-other" in listing  # unrelated stale registration kept


# --- CLI ---------------------------------------------------------------------


def test_workspace_cli_round_trip(env, capsys):
    registry = env["tmp"] / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "common_runtime_root": str(env["runtime"]),
                "goals": [{**env["goal"], "repo": str(env["tmp"]), "status": "active"}],
            }
        )
    )
    base = ["--registry", str(registry)]

    def run(*argv, fmt="json"):
        code = main([*base, "workspace", *argv, "--goal-id", GOAL, "--todo-id", TODO, "--format", fmt])
        out = capsys.readouterr().out
        return code, (json.loads(out) if fmt == "json" else out)

    code, prepared = run("prepare")
    assert code == 0 and set(prepared["paths"]) == {"api", "web", "docs"}
    commit_file(Path(prepared["paths"]["web"]), "web.txt", "client\n")
    code, text = run("status", fmt="markdown")
    assert code == 0 and "web: prepared=True" in text and "ahead=1" in text
    code, merged = run("merge", "--repo", "web")
    assert code == 0 and [item["name"] for item in merged["merged"]] == ["web"]
    code, cleaned = run("cleanup", "--repo", "web")
    assert code == 0 and cleaned["repos"][0]["action"] == "removed"
    code, missing = run("status", "--repo", "nope")
    assert code == 1 and missing["error_code"] == "unknown_repo"
    code = main([*base, "workspace", "status", "--goal-id", "missing", "--todo-id", TODO, "--format", "json"])
    assert code == 1 and json.loads(capsys.readouterr().out)["error_code"] == "goal_not_registered"


def test_workspace_cli_defaults_to_the_todos_task_repositories(env, capsys):
    """E2E pilot: `workspace merge` without --repo tried every Goal repo and was blocked."""

    from loopx.todos import add_goal_todo

    state = env["tmp"] / "ACTIVE_GOAL_STATE.md"
    state.write_text("# Goal\n\n## User Todo\n\n## Agent Todo\n\n## Completed Work Archive\n")
    registry = env["tmp"] / "registry.json"
    registry.write_text(json.dumps({"common_runtime_root": str(env["runtime"]), "goals": [
        {**env["goal"], "repo": str(env["tmp"]), "state_file": state.name, "status": "active"}]}))
    todo_id = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Web only",
                            role_contract={"task_repositories": ["web"]})["todo_id"]
    base = ["--registry", str(registry)]

    def run(*argv):
        code = main([*base, "workspace", *argv, "--goal-id", GOAL, "--todo-id", todo_id, "--format", "json"])
        return code, json.loads(capsys.readouterr().out)

    code, prepared = run("prepare")
    assert code == 0 and set(prepared["paths"]) == {"web"}
    commit_file(Path(prepared["paths"]["web"]), "web.txt", "client\n")
    code, merged = run("merge")
    assert code == 0 and [item["name"] for item in merged["merged"]] == ["web"]
