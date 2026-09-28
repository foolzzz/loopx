from __future__ import annotations

import json
from pathlib import Path

from loopx.control_plane.agents.workspace_guard import (
    build_delivery_workspace_guard,
    capture_delivery_workspace,
    delivery_workspace_identity,
    delivery_workspace_repository,
)


def test_gitless_single_agent_workspace_uses_stable_goal_identity(
    tmp_path: Path,
) -> None:
    project = tmp_path / "plain-project"
    project.mkdir()

    snapshot = capture_delivery_workspace(
        project,
        local_goal_id="plain-goal",
        local_project_root=project,
    )

    assert snapshot == {
        "schema_version": "delivery_workspace_v1",
        "workspace_identity": "loopx:plain-goal",
        "identity_kind": "local_goal",
        "task_repository": None,
        "repository_source": "goal_id_fallback",
        "workspace_kind": "local_goal_workspace",
        "peer_independent_worktree_required": False,
    }
    assert str(project) not in json.dumps(snapshot)
    assert delivery_workspace_identity(snapshot) == "loopx:plain-goal"
    assert delivery_workspace_repository(snapshot) is None
    assert build_delivery_workspace_guard(
        {"delivery_workspace": snapshot},
        current_path=tmp_path,
    ) is None


def test_gitless_workspace_fails_closed_outside_goal_or_for_peer_writes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "plain-project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()

    assert capture_delivery_workspace(
        outside,
        local_goal_id="plain-goal",
        local_project_root=project,
    ) is None
    assert capture_delivery_workspace(
        project,
        local_goal_id="plain-goal",
        local_project_root=project,
        peer_independent_worktree_required=True,
    ) is None


def test_legacy_git_workspace_remains_accepted() -> None:
    legacy = {
        "schema_version": "delivery_workspace_v0",
        "task_repository": "git:github.com/example/loopx",
        "repository_source": "current_git_origin",
        "workspace_kind": "canonical_checkout",
        "peer_independent_worktree_required": False,
    }

    assert delivery_workspace_identity(legacy) == "git:github.com/example/loopx"
    assert delivery_workspace_repository(legacy) == "git:github.com/example/loopx"


# ---------------------------------------------------------------------------
# Fork decision 29: the Todo workspace identity (E2E pilot gaps G1 and G3).
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from loopx.control_plane.agents.delivery_workspace import (  # noqa: E402
    normalize_delivery_workspace_snapshot,
)
from loopx.control_plane.agents.workspace_guard import (  # noqa: E402
    build_agent_workspace_guard,
    verify_todo_workspace,
)
from loopx.workspace import git_workspace  # noqa: E402
from tests.dispatch.dispatch_fixtures import git, git_env, make_repo  # noqa: E402

GOAL = "ws-identity"
SINGLE_REPO_SNAPSHOT_KEYS = {
    "schema_version", "workspace_identity", "identity_kind", "task_repository",
    "workspace_revision_digest", "repository_source", "workspace_kind",
    "peer_independent_worktree_required",
}


@pytest.fixture()
def s5(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    api, web = make_repo(tmp_path, "api"), make_repo(tmp_path, "web")
    runtime = tmp_path / "runtime"
    goal = {
        "id": GOAL,
        "repos": [
            {"name": "api", "path": str(api), "default_branch": "main"},
            {"name": "web", "path": str(web), "default_branch": "main"},
        ],
    }
    roots = {}
    for todo_id in ("todo_a", "todo_b"):
        prepared = git_workspace.prepare(goal, todo_id, None, runtime)
        assert prepared["ok"], prepared
        roots[todo_id] = Path(prepared["workspace_root"])
    return {"goal": goal, "runtime": runtime, "roots": roots, "api": api, "web": web, "tmp": tmp_path}


def _scope(s5: dict, todo_id: str = "todo_a") -> dict:
    return {"runtime_root": s5["runtime"], "goal_id": GOAL, "todo_id": todo_id, "goal": s5["goal"]}


def test_single_repo_delivery_with_origin_keeps_its_shape(s5: dict) -> None:
    git(s5["api"], "remote", "add", "origin", "https://github.com/example/api.git")
    snapshot = capture_delivery_workspace(
        s5["roots"]["todo_a"] / "api", peer_independent_worktree_required=True,
        todo_workspace_scope=_scope(s5),
    )
    assert snapshot is not None
    assert set(snapshot) == SINGLE_REPO_SNAPSHOT_KEYS
    assert snapshot["workspace_identity"] == "git:github.com/example/api"
    assert snapshot["repository_source"] == "current_git_origin"
    assert snapshot["workspace_kind"] == "independent_git_worktree"
    assert delivery_workspace_repository(snapshot) == "git:github.com/example/api"


def test_origin_less_repo_gets_a_stable_local_repo_id(s5: dict) -> None:
    worktree = capture_delivery_workspace(
        s5["roots"]["todo_a"] / "api", peer_independent_worktree_required=True,
    )
    checkout = capture_delivery_workspace(s5["api"])
    other = capture_delivery_workspace(s5["web"])
    assert worktree is not None and checkout is not None and other is not None
    repo_id = worktree["task_repository"]
    assert repo_id.startswith("local:") and len(repo_id) == len("local:") + 64
    # A checkout and its linked worktrees share the git common dir, so the id.
    assert checkout["task_repository"] == repo_id
    assert other["task_repository"] != repo_id
    assert worktree["repository_source"] == "current_git_common_dir"
    assert worktree["workspace_kind"] == "independent_git_worktree"
    assert set(worktree) == SINGLE_REPO_SNAPSHOT_KEYS
    assert str(s5["tmp"]) not in json.dumps(worktree)
    assert delivery_workspace_repository(worktree) == repo_id
    # The spend guard accepts the same local repository and refuses another.
    assert build_delivery_workspace_guard(
        {"delivery_workspace": worktree}, current_path=s5["roots"]["todo_b"] / "api",
    ) is None
    assert build_delivery_workspace_guard(
        {"delivery_workspace": worktree}, current_path=s5["roots"]["todo_a"] / "web",
    )["current_workspace"] == "foreign_git_worktree"


def test_the_registered_workspace_root_binds_a_todo_workspace_identity(s5: dict) -> None:
    root = s5["roots"]["todo_a"]
    snapshot = capture_delivery_workspace(
        root, peer_independent_worktree_required=True, todo_workspace_scope=_scope(s5),
    )
    assert snapshot is not None
    assert snapshot["identity_kind"] == "todo_workspace"
    assert snapshot["workspace_kind"] == "todo_workspace_root"
    assert snapshot["workspace_identity"] == f"todo-workspace:{GOAL}/todo_a"
    assert snapshot["task_repository"] is None
    assert snapshot["peer_independent_worktree_required"] is True
    identity = snapshot["todo_workspace"]
    assert identity["branch"] == f"loopx/{GOAL}/todo_a"
    assert [(repo["name"], repo["path"]) for repo in identity["repos"]] == [("api", "api"), ("web", "web")]
    for repo in identity["repos"]:
        assert repo["head_sha"] == git(root / repo["name"], "rev-parse", "HEAD")
    assert str(s5["tmp"]) not in json.dumps(snapshot)
    # Replay: the recorded snapshot normalizes to itself.
    assert normalize_delivery_workspace_snapshot(json.loads(json.dumps(snapshot))) == snapshot
    assert delivery_workspace_identity(snapshot) == f"todo-workspace:{GOAL}/todo_a"
    assert delivery_workspace_repository(snapshot) is None


def test_spoofed_todo_workspaces_are_rejected(s5: dict) -> None:
    roots = s5["roots"]
    # The root of a different Todo.
    assert capture_delivery_workspace(
        roots["todo_b"], peer_independent_worktree_required=True, todo_workspace_scope=_scope(s5),
    ) is None
    # A plain directory at the registered path of a Todo that was never prepared.
    fake = s5["runtime"] / "goals" / GOAL / "workspaces" / "todo_c"
    fake.mkdir()
    assert verify_todo_workspace(fake, runtime_root=s5["runtime"], goal_id=GOAL, todo_id="todo_c") is None
    # A repo off the Todo branch.
    git(roots["todo_a"] / "web", "checkout", "-q", "--detach")
    assert capture_delivery_workspace(
        roots["todo_a"], peer_independent_worktree_required=True, todo_workspace_scope=_scope(s5),
    ) is None
    git(roots["todo_a"] / "web", "checkout", "-q", f"loopx/{GOAL}/todo_a")
    assert capture_delivery_workspace(roots["todo_a"], todo_workspace_scope=_scope(s5)) is not None
    git(roots["todo_a"] / "web", "checkout", "-q", "-b", "elsewhere")
    assert capture_delivery_workspace(roots["todo_a"], todo_workspace_scope=_scope(s5)) is None
    git(roots["todo_a"] / "web", "checkout", "-q", f"loopx/{GOAL}/todo_a")
    # A worktree of a repository the Goal does not declare under that name.
    stranger = make_repo(s5["tmp"], "stranger")
    git(roots["todo_a"] / "web", "checkout", "-q", "--detach")
    git(s5["web"], "worktree", "remove", "--force", str(roots["todo_a"] / "web"))
    git(stranger, "worktree", "add", "-q", "-b", f"loopx/{GOAL}/todo_a", str(roots["todo_a"] / "web"))
    assert capture_delivery_workspace(roots["todo_a"], todo_workspace_scope=_scope(s5)) is None
    # Without a verified scope the root is still not a delivery workspace.
    assert capture_delivery_workspace(roots["todo_b"], peer_independent_worktree_required=True) is None


def test_the_spend_guard_requires_the_recorded_todo_workspace_root(s5: dict) -> None:
    snapshot = capture_delivery_workspace(
        s5["roots"]["todo_a"], peer_independent_worktree_required=True, todo_workspace_scope=_scope(s5),
    )
    run = {"delivery_workspace": snapshot}
    assert build_delivery_workspace_guard(
        run, current_path=s5["roots"]["todo_a"], runtime_root=s5["runtime"],
    ) is None
    for current in (s5["roots"]["todo_b"], s5["tmp"], s5["roots"]["todo_a"] / "api"):
        guard = build_delivery_workspace_guard(run, current_path=current, runtime_root=s5["runtime"])
        assert guard is not None and guard["blocks_delivery"] is True
        assert guard["required_workspace"] == "accountable_delivery_todo_workspace_root"
    # Without the runtime root the recorded workspace cannot be verified.
    assert build_delivery_workspace_guard(run, current_path=s5["roots"]["todo_a"]) is not None


def test_the_should_run_guard_accepts_only_this_todos_workspace(s5: dict) -> None:
    goal = {**s5["goal"], "repo": str(s5["tmp"] / "progress")}
    identity = {"agent_id": "dev", "registered_agents": ["orch", "dev", "acc"]}
    todo = {"todo_id": "todo_a", "action_kind": "implement", "task_repositories": ["api", "web"]}

    def guard(current: Path, selected: dict = todo):
        return build_agent_workspace_guard(
            goal, identity, selected_todo=selected, current_path=current, runtime_root=s5["runtime"],
        )

    assert guard(s5["roots"]["todo_a"]) is None
    assert guard(s5["roots"]["todo_b"]) is not None
    assert guard(s5["tmp"]) is not None
    single = {"todo_id": "todo_a", "action_kind": "implement", "task_repositories": ["api"]}
    assert guard(s5["roots"]["todo_a"] / "api", single) is None
    assert guard(s5["roots"]["todo_b"] / "api", single) is not None
    git(s5["roots"]["todo_a"] / "web", "checkout", "-q", "--detach")
    assert guard(s5["roots"]["todo_a"]) is not None
