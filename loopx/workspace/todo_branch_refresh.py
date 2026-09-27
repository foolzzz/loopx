"""Bring an untouched todo branch up to its merge target on release (pilot v1 gap N10).

A plan todo waits ``deferred`` until its dependencies are accepted and merged.
If its branch ``loopx/<goal>/<todo>`` was cut earlier (a manual ``workspace
prepare``, or a prepare before gap F1 was fixed), the branch still points at
the old merge target, and the developer would start from stale code and hit a
merge conflict at accept.

When the todo is released, each repo's todo branch that carries **no commits
of its own** (its tip is an ancestor of the current merge target) and is behind
that target is fast-forwarded to it. A branch with any commit that the target
lacks is never touched, and neither is a checked-out worktree with uncommitted
changes. Nothing is forced, reset or rebased: the only moves are a
fast-forward merge inside a clean worktree, or a compare-and-swap ref update
when no worktree holds the branch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import git_workspace as gw

TODO_BRANCH_REFRESH_SCHEMA_VERSION = "loopx_todo_branch_refresh_v0"


def _merge_target_ref(ctx: Mapping[str, Any]) -> str | None:
    """The branch a new todo branch would start from (see ``git_workspace.prepare``)."""

    repo_dir = ctx["path"]
    if ctx["merge_target"] == "task_branch" and gw._branch_exists(repo_dir, ctx["target_branch"]):
        return str(ctx["target_branch"])
    if gw._branch_exists(repo_dir, ctx["default_branch"]):
        return str(ctx["default_branch"])
    return None


def _refresh_one(repo: Mapping[str, Any], goal_id: str, todo_id: str, *, dry_run: bool) -> dict[str, Any]:
    ctx = gw._resolve_repo(repo, goal_id=goal_id)
    repo_dir = ctx["path"]
    branch = gw.todo_branch(goal_id, todo_id)
    result: dict[str, Any] = {"name": ctx["name"], "branch": branch}
    branch_sha = gw._rev(repo_dir, f"refs/heads/{branch}")
    if branch_sha is None:
        return {**result, "action": "no_branch"}
    target = _merge_target_ref(ctx)
    target_sha = gw._rev(repo_dir, f"refs/heads/{target}") if target else None
    if target is None or target_sha is None:
        return {**result, "action": "skipped", "reason": "merge_target_missing"}
    result.update({"target_branch": target, "from": branch_sha, "to": target_sha})
    if branch_sha == target_sha:
        return {**result, "action": "up_to_date"}
    if not gw._is_ancestor(repo_dir, branch_sha, target_sha):
        # Commits of its own (developer work) or a diverged target: never rewrite.
        return {**result, "action": "skipped", "reason": "branch_has_own_commits"}
    holder = gw._worktree_on_branch(gw._list_worktrees(repo_dir), branch)
    if holder is not None and Path(holder["path"]).is_dir():
        if gw._dirty_paths(holder["path"]):
            return {**result, "action": "skipped", "reason": "worktree_dirty"}
        if dry_run:
            return {**result, "action": "would_fast_forward"}
        gw._git(holder["path"], "merge", "--ff-only", "-q", target_sha)
    else:
        if dry_run:
            return {**result, "action": "would_fast_forward"}
        # Compare-and-swap: only moves the ref if nobody moved it meanwhile.
        gw._git(repo_dir, "update-ref", f"refs/heads/{branch}", target_sha, branch_sha)
    return {**result, "action": "fast_forwarded"}


def refresh_untouched_todo_branches(
    goal: Mapping[str, Any],
    todo_id: str,
    repo_names: Sequence[str] | None,
    runtime_root: str | Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fast-forward the todo's branches that have no commits of their own."""

    try:
        goal_id = gw._goal_id(goal)
        todo_id = gw._checked_id(todo_id, field="todo id")
        repos = gw._select_repos(goal, repo_names)
    except gw.WorkspaceError as exc:
        return {"schema_version": TODO_BRANCH_REFRESH_SCHEMA_VERSION, "ok": False, **exc.as_dict(), "repos": []}
    results: list[dict[str, Any]] = []
    with gw._goal_lock(runtime_root, goal_id):
        for repo in repos:
            try:
                results.append(_refresh_one(repo, goal_id, todo_id, dry_run=dry_run))
            except gw.WorkspaceError as exc:
                results.append({"name": repo.get("name"), "action": "error", **exc.as_dict()})
    return {
        "schema_version": TODO_BRANCH_REFRESH_SCHEMA_VERSION,
        "ok": all(item.get("action") != "error" for item in results),
        "goal_id": goal_id,
        "todo_id": todo_id,
        "dry_run": dry_run,
        "repos": results,
        "refreshed": [item["name"] for item in results if item.get("action") == "fast_forwarded"],
    }
