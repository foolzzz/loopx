"""Throwaway review checkouts for the acceptor (fork gap G12, design decision 35).

The acceptor only reviews. It never runs in the developer's todo worktree:
for each acceptor Turn the dispatcher creates one *detached* worktree per repo
at the commit the developer delivered, under

    <runtime_root>/goals/<goal>/reviews/<todo>/<attempt>/<repo>

Nothing the acceptor does there (edits, commits) can reach the todo branch
``loopx/<goal>/<todo>``: a detached HEAD moves no branch, and the accept merge
merges exactly the recorded delivered sha (see ``git_workspace.merge``). After
the Turn the checkout is inspected (changes or new commits are reported as a
warning) and removed. Removal is best-effort and idempotent.

The delivered sha per repo is recorded at delivery in the todo's delivery
evidence as ``delivered_shas=<repo>@<sha>,...`` (``format_delivered_shas``).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .git_workspace import (
    WorkspaceError,
    _checked_id,
    _drop_stale_registration,
    _git,
    _goal_id,
    _goal_lock,
    _list_worktrees,
    _resolve_repo,
    _rev,
    _select_repos,
    _worktree_at,
    todo_branch,
)

REVIEW_CHECKOUT_SCHEMA_VERSION = "loopx_review_checkout_v0"
DELIVERED_SHAS_KEY = "delivered_shas"
_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_DELIVERED_SHAS = re.compile(
    r"(?:^|[;\s])delivered_shas=((?:[A-Za-z0-9][A-Za-z0-9._-]*@[0-9a-f]{40,64},?)+)"
)


# ---------------------------------------------------------------------------
# delivered shas
# ---------------------------------------------------------------------------


def delivered_branch_shas(
    goal: Mapping[str, Any], todo_id: str, repo_names: Sequence[str] | None,
) -> dict[str, str]:
    """The tip of ``loopx/<goal>/<todo>`` per repo, for the repos that have it."""

    try:
        goal_id = _goal_id(goal)
        todo_id = _checked_id(todo_id, field="todo id")
        repos = _select_repos(goal, repo_names)
    except WorkspaceError:
        return {}
    branch = todo_branch(goal_id, todo_id)
    shas: dict[str, str] = {}
    for repo in repos:
        try:
            ctx = _resolve_repo(repo, goal_id=goal_id)
        except WorkspaceError:
            continue
        sha = _rev(ctx["path"], f"refs/heads/{branch}")
        if sha:
            shas[str(repo["name"])] = sha.lower()
    return shas


def format_delivered_shas(shas: Mapping[str, str]) -> str:
    return ",".join(f"{name}@{sha}" for name, sha in sorted(shas.items()))


def parse_delivered_shas(evidence: Any) -> dict[str, str]:
    """Read ``delivered_shas=`` from a todo's delivery evidence (empty when absent)."""

    match = _DELIVERED_SHAS.search(str(evidence or ""))
    if not match:
        return {}
    shas: dict[str, str] = {}
    for item in match.group(1).split(","):
        name, _, sha = item.partition("@")
        if name and _SHA.fullmatch(sha):
            shas[name] = sha
    return shas


# ---------------------------------------------------------------------------
# review checkout lifecycle
# ---------------------------------------------------------------------------


def review_checkout_root(runtime_root: str | Path, goal_id: str, todo_id: str, attempt: str) -> Path:
    return (
        Path(runtime_root).expanduser() / "goals" / goal_id / "reviews"
        / _checked_id(todo_id, field="todo id") / _checked_id(attempt, field="review attempt")
    )


def prepare_review_checkout(
    goal: Mapping[str, Any],
    todo_id: str,
    delivered: Mapping[str, str],
    runtime_root: str | Path,
    *,
    attempt: str,
) -> dict[str, Any]:
    """Create one detached worktree per delivered repo at its delivered sha."""

    payload: dict[str, Any] = {
        "schema_version": REVIEW_CHECKOUT_SCHEMA_VERSION, "action": "prepare",
        "todo_id": str(todo_id), "attempt": str(attempt), "repos": [], "paths": {},
    }
    try:
        goal_id = _goal_id(goal)
        root = review_checkout_root(runtime_root, goal_id, todo_id, attempt)
        if not delivered:
            raise WorkspaceError("delivery_unrecorded", "no delivered sha is recorded for this todo")
        repos = _select_repos(goal, sorted(delivered))
    except WorkspaceError as exc:
        return {**payload, "ok": False, **exc.as_dict()}
    payload.update(goal_id=goal_id, workspace_root=str(root))
    with _goal_lock(runtime_root, goal_id):
        for repo in repos:
            name = str(repo["name"])
            sha = str(delivered[name])
            path = root / name
            try:
                ctx = _resolve_repo(repo, goal_id=goal_id)
                if _rev(ctx["path"], sha) is None:
                    raise WorkspaceError("delivered_sha_missing", f"delivered commit {sha[:12]} is not in repo {name}")
                if path.exists():
                    if _worktree_at(_list_worktrees(ctx["path"]), path) is None:
                        raise WorkspaceError("path_occupied", f"{path} exists and is not a worktree of {name}")
                    head = _rev(path, "HEAD")
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _git(ctx["path"], "worktree", "add", "--detach", str(path), sha)
                    head = _rev(path, "HEAD")
                payload["repos"].append({"name": name, "ok": True, "path": str(path), "sha": sha, "head": head})
                payload["paths"][name] = str(path)
            except WorkspaceError as exc:
                payload["repos"].append({"name": name, "ok": False, "sha": sha, **exc.as_dict()})
    payload["ok"] = bool(payload["repos"]) and all(item["ok"] for item in payload["repos"])
    return payload


def inspect_review_checkout(
    goal: Mapping[str, Any], checkout: Mapping[str, Any],
) -> dict[str, Any]:
    """Report whether the acceptor changed its review checkout.

    ``modified`` is true when any repo has uncommitted changes or its HEAD is
    no longer the delivered sha (new commits or a switched branch).
    """

    repos: list[dict[str, Any]] = []
    for item in checkout.get("repos") or []:
        if not isinstance(item, Mapping) or not item.get("ok"):
            continue
        path = Path(str(item.get("path") or ""))
        record: dict[str, Any] = {"name": item.get("name"), "sha": item.get("sha")}
        if not path.is_dir():
            record.update(missing=True, dirty_paths=[], new_commits=False)
        else:
            try:
                status = _git(path, "status", "--porcelain").stdout.splitlines()
                head = _rev(path, "HEAD")
            except WorkspaceError as exc:
                record.update(error_code=exc.code)
                repos.append(record)
                continue
            record.update(
                dirty_paths=[line[3:] for line in status if line.strip()][:20],
                new_commits=bool(head and head != item.get("sha")),
                head=head,
            )
        record["modified"] = bool(record.get("dirty_paths") or record.get("new_commits"))
        repos.append(record)
    return {"modified": any(item.get("modified") for item in repos), "repos": repos}


def remove_review_checkout(
    goal: Mapping[str, Any],
    todo_id: str,
    runtime_root: str | Path,
    *,
    attempt: str,
) -> dict[str, Any]:
    """Remove the review checkout of one attempt. Best-effort and idempotent."""

    removed: list[str] = []
    failures: list[dict[str, Any]] = []
    try:
        goal_id = _goal_id(goal)
        root = review_checkout_root(runtime_root, goal_id, todo_id, attempt)
        repos = _select_repos(goal, None)
    except WorkspaceError as exc:
        return {"ok": False, **exc.as_dict(), "removed": removed}
    with _goal_lock(runtime_root, goal_id):
        for repo in repos:
            path = root / str(repo["name"])
            try:
                ctx = _resolve_repo(repo, goal_id=goal_id)
                registered = _worktree_at(_list_worktrees(ctx["path"]), path)
                if registered is None:
                    continue
                if path.is_dir():
                    _git(ctx["path"], "worktree", "remove", "--force", str(path))
                else:
                    _drop_stale_registration(ctx["path"], path)
                removed.append(str(repo["name"]))
            except WorkspaceError as exc:
                failures.append({"name": repo["name"], **exc.as_dict()})
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        parent = root.parent
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
    return {"ok": not failures and not root.exists(), "removed": removed, "failures": failures}
