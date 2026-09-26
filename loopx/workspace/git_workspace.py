"""Per-todo git worktrees across a Goal's repos, with an atomic merge.

Layout: ``<runtime_root>/goals/<goal>/workspaces/<todo>/<repo_name>`` holds one
linked worktree per repo, all on the same branch ``loopx/<goal>/<todo>``.

``merge`` is all-or-nothing across the selected repos: every repo is checked
with ``git merge-tree`` first (no working tree is touched); any conflict or
blocker merges nothing and returns a structured report for the developer. A
failure while applying rolls already merged repos back to their previous
heads. Nothing here ever fetches or pushes; pushing is a user gate.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .repos import goal_repos

GIT_WORKSPACE_SCHEMA_VERSION = "loopx_git_workspace_v0"
BRANCH_PREFIX = "loopx"
DEFAULT_TASK_BRANCH_PREFIX = "loopx-task"
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FALLBACK_IDENTITY = {
    "GIT_AUTHOR_NAME": "LoopX",
    "GIT_AUTHOR_EMAIL": "loopx@localhost",
    "GIT_COMMITTER_NAME": "LoopX",
    "GIT_COMMITTER_EMAIL": "loopx@localhost",
}


class WorkspaceError(Exception):
    def __init__(self, code: str, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"error_code": self.code, "reason": self.reason, **self.details}


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def _git_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    if extra:
        env.update(extra)
    return env


def _git(
    cwd: str | Path,
    *args: str,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=_git_env(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise WorkspaceError("git_unavailable", f"cannot run git in {cwd}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError("git_timeout", f"git {' '.join(args)} timed out in {cwd}") from exc
    if check and completed.returncode != 0:
        raise WorkspaceError(
            "git_failed",
            f"git {' '.join(args)} failed in {cwd}: "
            f"{(completed.stderr or completed.stdout).strip()}",
        )
    return completed


def _rev(cwd: str | Path, ref: str) -> str | None:
    completed = _git(cwd, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _branch_exists(cwd: str | Path, branch: str) -> bool:
    return _rev(cwd, f"refs/heads/{branch}") is not None


def _is_ancestor(cwd: str | Path, ancestor: str, descendant: str) -> bool:
    return _git(cwd, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.realpath(str(left)) == os.path.realpath(str(right))


def _list_worktrees(repo_dir: str | Path) -> list[dict[str, Any]]:
    output = _git(repo_dir, "worktree", "list", "--porcelain").stdout
    worktrees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in output.splitlines() + [""]:
        if not line:
            if current:
                worktrees.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": value, "branch": None, "detached": False}
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value
        elif key in ("detached", "bare", "locked", "prunable"):
            current[key] = value or True
    return worktrees


def _worktree_at(worktrees: Sequence[Mapping[str, Any]], path: Path) -> Mapping[str, Any] | None:
    return next((wt for wt in worktrees if _same_path(wt["path"], path)), None)


def _worktree_on_branch(
    worktrees: Sequence[Mapping[str, Any]], branch: str
) -> Mapping[str, Any] | None:
    ref = f"refs/heads/{branch}"
    return next((wt for wt in worktrees if wt.get("branch") == ref and not wt.get("bare")), None)


def _dirty_paths(worktree: str | Path, *, include_untracked: bool = True) -> list[str]:
    args = ["status", "--porcelain"]
    if not include_untracked:
        args.append("--untracked-files=no")
    return [line[3:] for line in _git(worktree, *args).stdout.splitlines() if line.strip()]


def _unmerged_paths(worktree: str | Path) -> list[str]:
    output = _git(worktree, "diff", "--name-only", "--diff-filter=U").stdout
    return sorted({line for line in output.splitlines() if line.strip()})


def _merge_check(repo_dir: str | Path, target_sha: str, source_sha: str) -> dict[str, Any]:
    """Predict merging ``source`` into ``target`` without touching any worktree."""

    if _is_ancestor(repo_dir, source_sha, target_sha):
        return {"state": "up_to_date", "conflicted_paths": [], "messages": []}
    completed = _git(
        repo_dir, "merge-tree", "--write-tree", "--name-only", target_sha, source_sha, check=False
    )
    if completed.returncode not in (0, 1):
        raise WorkspaceError(
            "merge_check_failed",
            f"git merge-tree failed in {repo_dir}: {completed.stderr.strip()}",
        )
    lines = completed.stdout.splitlines()
    tree = lines[0].strip() if lines else ""
    if completed.returncode == 0:
        return {"state": "clean", "tree": tree, "conflicted_paths": [], "messages": []}
    rest = lines[1:]
    split = rest.index("") if "" in rest else len(rest)
    return {
        "state": "conflict",
        "conflicted_paths": sorted({item for item in rest[:split] if item}),
        "messages": [item for item in rest[split + 1 :] if item.strip()],
    }


def _drop_stale_registration(repo_dir: str | Path, path: Path) -> None:
    """Forget the registration of exactly ``path`` whose directory is gone.

    ``git worktree prune`` would also forget unrelated stale worktrees, so the
    one admin entry pointing at ``path`` is removed instead.
    """

    common = _git(repo_dir, "rev-parse", "--git-common-dir").stdout.strip()
    admin_root = Path(common if os.path.isabs(common) else os.path.join(str(repo_dir), common)) / "worktrees"
    if not admin_root.is_dir():
        return
    for admin in admin_root.iterdir():
        gitdir_file = admin / "gitdir"
        if not gitdir_file.is_file():
            continue
        registered = gitdir_file.read_text(encoding="utf-8").strip()
        if _same_path(os.path.dirname(registered), path) and not path.exists():
            shutil.rmtree(admin)
            return


def _commit_identity_env(repo_dir: str | Path) -> dict[str, str]:
    if _git(repo_dir, "var", "GIT_COMMITTER_IDENT", check=False).returncode == 0 and _git(
        repo_dir, "var", "GIT_AUTHOR_IDENT", check=False
    ).returncode == 0:
        return {}
    return dict(_FALLBACK_IDENTITY)


# ---------------------------------------------------------------------------
# naming and repo resolution
# ---------------------------------------------------------------------------


def _checked_id(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not _ID_PATTERN.match(text) or ".." in text or text.endswith((".lock", ".")):
        raise WorkspaceError("invalid_id", f"{field} is not usable in a path or branch name: {text!r}")
    return text


def todo_branch(goal_id: str, todo_id: str) -> str:
    return f"{BRANCH_PREFIX}/{goal_id}/{todo_id}"


def default_task_branch(goal_id: str) -> str:
    # A sibling namespace: ``loopx/<goal>`` itself cannot be a branch while
    # ``loopx/<goal>/<todo>`` branches exist.
    return f"{DEFAULT_TASK_BRANCH_PREFIX}/{goal_id}"


def todo_workspace_root(runtime_root: str | Path, goal_id: str, todo_id: str) -> Path:
    return Path(runtime_root).expanduser() / "goals" / goal_id / "workspaces" / todo_id


def _goal_id(goal: Mapping[str, Any]) -> str:
    return _checked_id((goal or {}).get("id"), field="goal id")


def _select_repos(goal: Mapping[str, Any], repo_names: Sequence[str] | None) -> list[dict[str, Any]]:
    try:
        repos = goal_repos(goal)
    except ValueError as exc:
        raise WorkspaceError("invalid_repo_config", str(exc)) from exc
    if not repos:
        raise WorkspaceError(
            "no_repos_configured",
            "the Goal declares no repos; add one with `loopx configure-goal --repo NAME=/abs/path --execute`",
        )
    if not repo_names:
        return repos
    by_name = {repo["name"]: repo for repo in repos}
    unknown = [name for name in repo_names if name not in by_name]
    if unknown:
        raise WorkspaceError(
            "unknown_repo",
            f"repo(s) not declared by the Goal: {', '.join(unknown)}",
            declared=sorted(by_name),
        )
    return [by_name[name] for name in dict.fromkeys(repo_names)]


def _default_branch(repo_dir: str, configured: str | None) -> str:
    if configured:
        if not _branch_exists(repo_dir, configured):
            raise WorkspaceError(
                "default_branch_missing",
                f"default branch {configured!r} does not exist locally in {repo_dir}",
            )
        return configured
    origin_head = _git(
        repo_dir, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False
    ).stdout.strip()
    candidates = [origin_head.split("/", 1)[1]] if "/" in origin_head else []
    candidates += ["main", "master"]
    for candidate in candidates:
        if _branch_exists(repo_dir, candidate):
            return candidate
    current = _git(repo_dir, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    if current and _branch_exists(repo_dir, current):
        return current
    raise WorkspaceError(
        "default_branch_unresolved",
        f"cannot resolve a default branch in {repo_dir}; set default_branch in the Goal repo entry",
    )


def _resolve_repo(repo: Mapping[str, Any], *, goal_id: str) -> dict[str, Any]:
    path = str(repo.get("path") or "")
    if not path or not os.path.isdir(path):
        raise WorkspaceError("repo_missing", f"repo path does not exist: {path!r}")
    probe = _git(path, "rev-parse", "--is-inside-work-tree", check=False)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise WorkspaceError("not_a_git_repo", f"repo path is not a git work tree: {path}")
    default_branch = _default_branch(path, repo.get("default_branch"))
    if repo.get("merge_target") == "task_branch":
        target_branch = str(repo.get("task_branch") or default_task_branch(goal_id))
    else:
        target_branch = default_branch
    return {
        "name": repo["name"],
        "path": path,
        "default_branch": default_branch,
        "merge_target": repo.get("merge_target") or "main",
        "target_branch": target_branch,
    }


@contextmanager
def _goal_lock(runtime_root: str | Path, goal_id: str) -> Iterator[None]:
    lock_dir = Path(runtime_root).expanduser() / "goals" / goal_id / "workspaces"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = open(lock_dir / ".lock", "a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - non-POSIX host
            pass
        yield
    finally:
        handle.close()


def _envelope(action: str, goal_id: str, todo_id: str, *, dry_run: bool) -> dict[str, Any]:
    return {
        "schema_version": GIT_WORKSPACE_SCHEMA_VERSION,
        "action": action,
        "goal_id": goal_id,
        "todo_id": todo_id,
        "branch": todo_branch(goal_id, todo_id),
        "dry_run": dry_run,
    }


def _error_payload(action: str, goal: Mapping[str, Any], todo_id: Any, exc: WorkspaceError, dry_run: bool) -> dict[str, Any]:
    return {
        "schema_version": GIT_WORKSPACE_SCHEMA_VERSION,
        "action": action,
        "goal_id": str((goal or {}).get("id") or ""),
        "todo_id": str(todo_id or ""),
        "dry_run": dry_run,
        "ok": False,
        **exc.as_dict(),
        "repos": [],
    }


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def prepare(
    goal: Mapping[str, Any],
    todo_id: str,
    repo_names: Sequence[str] | None,
    runtime_root: str | Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create or reuse one worktree per repo on the shared todo branch."""

    try:
        goal_id = _goal_id(goal)
        todo_id = _checked_id(todo_id, field="todo id")
        repos = _select_repos(goal, repo_names)
    except WorkspaceError as exc:
        return _error_payload("prepare", goal, todo_id, exc, dry_run)
    payload = _envelope("prepare", goal_id, todo_id, dry_run=dry_run)
    root = todo_workspace_root(runtime_root, goal_id, todo_id)
    results = []
    with _goal_lock(runtime_root, goal_id):
        for repo in repos:
            try:
                results.append(_prepare_one(repo, goal_id, todo_id, root, dry_run=dry_run))
            except WorkspaceError as exc:
                results.append({"name": repo["name"], "ok": False, **exc.as_dict()})
    payload["ok"] = all(item["ok"] for item in results)
    payload["workspace_root"] = str(root)
    payload["repos"] = results
    payload["paths"] = {item["name"]: item["path"] for item in results if item.get("ok")}
    return payload


def _prepare_one(
    repo: Mapping[str, Any], goal_id: str, todo_id: str, root: Path, *, dry_run: bool
) -> dict[str, Any]:
    ctx = _resolve_repo(repo, goal_id=goal_id)
    repo_dir = ctx["path"]
    branch = todo_branch(goal_id, todo_id)
    path = root / ctx["name"]
    result = {"name": ctx["name"], "path": str(path), "branch": branch, **_ctx_fields(ctx)}
    worktrees = _list_worktrees(repo_dir)
    existing = _worktree_at(worktrees, path)
    stale = False
    if existing is not None:
        if existing.get("branch") != f"refs/heads/{branch}":
            raise WorkspaceError(
                "worktree_path_conflict",
                f"{path} is a worktree of {repo_dir} on "
                f"{existing.get('branch') or 'a detached HEAD'}, not {branch}",
                path=str(path),
            )
        if path.is_dir():
            return {**result, "ok": True, "action": "reused", "head": _rev(path, "HEAD")}
        stale = True  # registered for our exact path but the directory is gone
    elif path.exists():
        raise WorkspaceError(
            "path_occupied", f"{path} exists and is not a worktree of {repo_dir}", path=str(path)
        )
    holder = _worktree_on_branch(worktrees, branch)
    if holder is not None and not _same_path(holder["path"], path):
        raise WorkspaceError(
            "branch_checked_out_elsewhere",
            f"{branch} is already checked out at {holder['path']}",
            holder_path=holder["path"],
        )
    branch_exists = _branch_exists(repo_dir, branch)
    base = ctx["default_branch"]
    create_task_branch = False
    if ctx["merge_target"] == "task_branch":
        if _branch_exists(repo_dir, ctx["target_branch"]):
            base = ctx["target_branch"]
        else:
            create_task_branch = True
    action = "attach_existing_branch" if branch_exists else "create"
    result.update({"base_branch": base, "task_branch_created": create_task_branch})
    if dry_run:
        return {**result, "ok": True, "action": f"would_{action}"}
    if create_task_branch:
        _git(repo_dir, "branch", ctx["target_branch"], ctx["default_branch"])
        base = ctx["target_branch"]
        result["base_branch"] = base
    path.parent.mkdir(parents=True, exist_ok=True)
    force = ["-f"] if stale else []
    if branch_exists:
        _git(repo_dir, "worktree", "add", *force, str(path), branch)
    else:
        _git(repo_dir, "worktree", "add", *force, "-b", branch, str(path), base)
    return {**result, "ok": True, "action": action, "head": _rev(path, "HEAD")}


def _ctx_fields(ctx: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "repo_path": ctx["path"],
        "default_branch": ctx["default_branch"],
        "merge_target": ctx["merge_target"],
        "target_branch": ctx["target_branch"],
    }


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def status(
    goal: Mapping[str, Any],
    todo_id: str,
    repo_names: Sequence[str] | None,
    runtime_root: str | Path,
) -> dict[str, Any]:
    """Per repo: prepared, dirty, ahead/behind the merge target, conflicts."""

    try:
        goal_id = _goal_id(goal)
        todo_id = _checked_id(todo_id, field="todo id")
        repos = _select_repos(goal, repo_names)
    except WorkspaceError as exc:
        return _error_payload("status", goal, todo_id, exc, False)
    payload = _envelope("status", goal_id, todo_id, dry_run=False)
    root = todo_workspace_root(runtime_root, goal_id, todo_id)
    results = []
    for repo in repos:
        try:
            results.append(_status_one(repo, goal_id, todo_id, root))
        except WorkspaceError as exc:
            results.append({"name": repo["name"], "ok": False, **exc.as_dict()})
    payload["ok"] = all(item["ok"] for item in results)
    payload["repos"] = results
    payload["mergeable"] = bool(results) and all(
        item.get("ok") and item.get("merge_blockers") == [] for item in results
    )
    return payload


def _status_one(repo: Mapping[str, Any], goal_id: str, todo_id: str, root: Path) -> dict[str, Any]:
    ctx = _resolve_repo(repo, goal_id=goal_id)
    repo_dir = ctx["path"]
    branch = todo_branch(goal_id, todo_id)
    path = root / ctx["name"]
    worktree = _worktree_at(_list_worktrees(repo_dir), path)
    source_sha = _rev(repo_dir, f"refs/heads/{branch}")
    target_sha = _rev(repo_dir, f"refs/heads/{ctx['target_branch']}")
    base_sha = target_sha or _rev(repo_dir, f"refs/heads/{ctx['default_branch']}")
    result: dict[str, Any] = {
        "name": ctx["name"],
        "ok": True,
        "path": str(path),
        "branch": branch,
        **_ctx_fields(ctx),
        "prepared": worktree is not None and path.is_dir(),
        "branch_exists": source_sha is not None,
        "target_exists": target_sha is not None,
        "head": source_sha,
    }
    blockers: list[str] = []
    if worktree is not None and path.is_dir():
        detached = bool(worktree.get("detached"))
        on_branch = worktree.get("branch") == f"refs/heads/{branch}"
        dirty = _dirty_paths(path)
        unmerged = _unmerged_paths(path)
        result.update(
            {
                "detached": detached,
                "on_todo_branch": on_branch,
                "dirty": bool(dirty),
                "dirty_paths": dirty[:50],
                "unmerged_paths": unmerged,
            }
        )
        if detached or not on_branch:
            blockers.append("worktree_not_on_todo_branch")
        if dirty:
            blockers.append("worktree_dirty")
        if unmerged:
            blockers.append("worktree_unmerged_paths")
    if source_sha is None:
        blockers.append("todo_branch_missing")
    elif base_sha is not None:
        counts = _git(repo_dir, "rev-list", "--left-right", "--count", f"{base_sha}...{source_sha}").stdout.split()
        result["behind"], result["ahead"] = int(counts[0]), int(counts[1])
        check = _merge_check(repo_dir, base_sha, source_sha)
        result["merge_check"] = {key: value for key, value in check.items() if key != "tree"}
        if check["state"] == "conflict":
            blockers.append("merge_conflict")
    result["merge_blockers"] = blockers
    return result


# ---------------------------------------------------------------------------
# merge (atomic across repos)
# ---------------------------------------------------------------------------


def repos_with_todo_branch(goal: Mapping[str, Any], todo_id: str, repo_names: Sequence[str] | None) -> list[str]:
    """The selected repos in which the todo branch ``loopx/<goal>/<todo>`` exists.

    Merge eligibility keys on the branch, not on the worktree directory: the
    branch carries the work even after its worktree is gone. Unresolvable
    repos are left out; the merge itself reports them as blockers.
    """

    try:
        goal_id = _goal_id(goal)
        todo_id = _checked_id(todo_id, field="todo id")
        repos = _select_repos(goal, repo_names)
    except WorkspaceError:
        return []
    branch = todo_branch(goal_id, todo_id)
    found: list[str] = []
    for repo in repos:
        try:
            ctx = _resolve_repo(repo, goal_id=goal_id)
        except WorkspaceError:
            continue
        if _rev(ctx["path"], f"refs/heads/{branch}") is not None:
            found.append(str(repo["name"]))
    return found


def merge(
    goal: Mapping[str, Any],
    todo_id: str,
    repo_names: Sequence[str] | None,
    runtime_root: str | Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Merge the todo branch into each repo's target, all repos or none."""

    try:
        goal_id = _goal_id(goal)
        todo_id = _checked_id(todo_id, field="todo id")
        repos = _select_repos(goal, repo_names)
    except WorkspaceError as exc:
        return _error_payload("merge", goal, todo_id, exc, dry_run)
    payload = _envelope("merge", goal_id, todo_id, dry_run=dry_run)
    root = todo_workspace_root(runtime_root, goal_id, todo_id)
    with _goal_lock(runtime_root, goal_id):
        plans = [_merge_preflight(repo, goal_id, todo_id, root) for repo in repos]
        payload["repos"] = [_public_plan(plan) for plan in plans]
        blocked = [plan for plan in plans if plan["blockers"]]
        if blocked:
            payload.update(
                {
                    "ok": False,
                    "error_code": "merge_blocked",
                    "merged": [],
                    "conflict_report": _conflict_report(goal_id, todo_id, blocked),
                }
            )
            return payload
        if dry_run:
            payload.update({"ok": True, "merged": [], "would_merge": [p["name"] for p in plans if p["state"] == "clean"]})
            return payload
        repo_list = [plan["name"] for plan in plans]
        applied: list[dict[str, Any]] = []
        failure: dict[str, Any] | None = None
        for plan in plans:
            if plan["state"] != "clean":
                continue
            try:
                message = _merge_message(goal_id, todo_id, repo_list, plan["target_branch"])
                applied.append(_apply_repo_merge(plan, message))
            except Exception as exc:  # noqa: BLE001 - every failure must roll back
                failure = {
                    "name": plan["name"],
                    **(exc.as_dict() if isinstance(exc, WorkspaceError) else {"error_code": "apply_failed", "reason": str(exc)}),
                }
                break
        if failure is None:
            payload.update({"ok": True, "merged": applied})
            return payload
        rolled_back, rollback_failures = _rollback(applied)
        payload.update(
            {
                "ok": False,
                "error_code": "merge_apply_failed",
                "failure": failure,
                "merged": [],
                "rolled_back": rolled_back,
                "rollback_failures": rollback_failures,
            }
        )
        return payload


def _merge_preflight(repo: Mapping[str, Any], goal_id: str, todo_id: str, root: Path) -> dict[str, Any]:
    branch = todo_branch(goal_id, todo_id)
    plan: dict[str, Any] = {"name": repo["name"], "branch": branch, "blockers": [], "state": "blocked"}
    try:
        ctx = _resolve_repo(repo, goal_id=goal_id)
    except WorkspaceError as exc:
        plan["blockers"].append(exc.as_dict())
        return plan
    plan.update(_ctx_fields(ctx))
    repo_dir = ctx["path"]
    worktrees = _list_worktrees(repo_dir)
    source_sha = _rev(repo_dir, f"refs/heads/{branch}")
    if source_sha is None:
        plan["blockers"].append({"error_code": "todo_branch_missing", "reason": f"{branch} does not exist"})
        return plan
    todo_path = root / ctx["name"]
    todo_wt = _worktree_at(worktrees, todo_path)
    if todo_wt is not None and todo_path.is_dir():
        if todo_wt.get("detached") or todo_wt.get("branch") != f"refs/heads/{branch}":
            plan["blockers"].append(
                {"error_code": "todo_worktree_detached", "reason": f"{todo_path} is not on {branch}; commits made there are not on the todo branch"}
            )
        unmerged = _unmerged_paths(todo_path)
        dirty = _dirty_paths(todo_path)
        if unmerged:
            plan["blockers"].append(
                {"error_code": "todo_worktree_unmerged", "reason": "unresolved merge in the todo worktree", "paths": unmerged}
            )
        elif dirty:
            plan["blockers"].append(
                {"error_code": "todo_worktree_dirty", "reason": "uncommitted changes would be left out of the merge", "paths": dirty[:50]}
            )
    target = ctx["target_branch"]
    target_sha = _rev(repo_dir, f"refs/heads/{target}")
    base_sha = target_sha or _rev(repo_dir, f"refs/heads/{ctx['default_branch']}")
    target_wt = _worktree_on_branch(worktrees, target)
    if target_wt is not None:
        if target_wt.get("prunable") or not os.path.isdir(target_wt["path"]):
            target_wt = None
        else:
            dirty_target = _dirty_paths(target_wt["path"], include_untracked=False)
            if dirty_target or _unmerged_paths(target_wt["path"]):
                plan["blockers"].append(
                    {
                        "error_code": "target_checkout_dirty",
                        "reason": f"{target} is checked out at {target_wt['path']} with uncommitted changes",
                        "paths": dirty_target[:50],
                    }
                )
    plan.update(
        {
            "repo_dir": repo_dir,
            "source_sha": source_sha,
            "target_sha": target_sha,
            "base_sha": base_sha,
            "target_worktree": target_wt["path"] if target_wt else None,
        }
    )
    check = _merge_check(repo_dir, base_sha, source_sha)
    plan["check"] = check
    if check["state"] == "conflict":
        plan["blockers"].append(
            {
                "error_code": "merge_conflict",
                "reason": f"{branch} conflicts with {target}",
                "conflicted_paths": check["conflicted_paths"],
                "messages": check["messages"],
            }
        )
    if not plan["blockers"]:
        plan["state"] = "up_to_date" if check["state"] == "up_to_date" and target_sha else "clean"
    return plan


def _public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "name", "branch", "state", "blockers", "repo_path", "default_branch",
        "merge_target", "target_branch", "source_sha", "target_sha", "target_worktree",
    )
    return {key: plan.get(key) for key in keys if key in plan}


def _conflict_report(goal_id: str, todo_id: str, blocked: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    branch = todo_branch(goal_id, todo_id)
    return {
        "kind": "multi_repo_merge_blocked",
        "goal_id": goal_id,
        "todo_id": todo_id,
        "branch": branch,
        "merged_nothing": True,
        "repos": [
            {"name": plan["name"], "target_branch": plan.get("target_branch"), "blockers": plan["blockers"]}
            for plan in blocked
        ],
        "developer_instruction": (
            f"Nothing was merged in any repo. In each listed repo's todo worktree, "
            f"merge its target branch into {branch}, resolve and commit (or fix the "
            "listed blocker), then request the merge again."
        ),
    }


def _merge_message(goal_id: str, todo_id: str, repo_names: Sequence[str], target: str) -> str:
    return (
        f"Merge {todo_branch(goal_id, todo_id)} into {target}\n\n"
        f"LoopX-Goal: {goal_id}\n"
        f"LoopX-Todo: {todo_id}\n"
        f"LoopX-Repos: {', '.join(repo_names)}\n"
    )


def _apply_repo_merge(plan: Mapping[str, Any], message: str) -> dict[str, Any]:
    """Create the no-ff merge commit and move the target branch to it."""

    repo_dir = plan["repo_dir"]
    target = plan["target_branch"]
    if plan["check"]["state"] == "up_to_date":
        # Only reachable when the task branch does not exist yet and the todo
        # branch adds nothing: create the task branch at its base.
        commit = plan["base_sha"]
    else:
        commit = _git(
            repo_dir,
            "commit-tree", plan["check"]["tree"],
            "-p", plan["base_sha"], "-p", plan["source_sha"],
            "-m", message,
            env=_commit_identity_env(repo_dir),
        ).stdout.strip()
    ref = f"refs/heads/{target}"
    record = {
        "name": plan["name"], "target_branch": target, "repo_dir": repo_dir,
        "old": plan["target_sha"], "new": commit, "worktree": plan.get("target_worktree"),
    }
    if plan["target_sha"] is None:
        _git(repo_dir, "update-ref", "-m", "loopx workspace merge", ref, commit, "")
        record["mode"] = "created_ref"
    elif plan.get("target_worktree"):
        worktree = plan["target_worktree"]
        if _rev(worktree, "HEAD") != plan["target_sha"]:
            raise WorkspaceError("target_moved", f"{target} moved after the merge check")
        _git(worktree, "merge", "--ff-only", "--no-edit", commit)
        record["mode"] = "worktree_ff"
    else:
        _git(repo_dir, "update-ref", "-m", "loopx workspace merge", ref, commit, plan["target_sha"])
        record["mode"] = "ref"
    return record


def _rollback(applied: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rolled_back: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for record in reversed(applied):
        ref = f"refs/heads/{record['target_branch']}"
        try:
            if record["mode"] == "created_ref":
                _git(record["repo_dir"], "update-ref", "-d", ref, record["new"])
            elif record["mode"] == "worktree_ff":
                _git(record["worktree"], "reset", "--keep", record["old"])
            else:
                _git(record["repo_dir"], "update-ref", "-m", "loopx workspace rollback", ref, record["old"], record["new"])
            rolled_back.append({"name": record["name"], "target_branch": record["target_branch"], "restored_to": record["old"]})
        except WorkspaceError as exc:
            failures.append({"name": record["name"], **exc.as_dict()})
    return rolled_back, failures


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


def cleanup(
    goal: Mapping[str, Any],
    todo_id: str,
    repo_names: Sequence[str] | None,
    runtime_root: str | Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove this todo's worktrees and branches once merged (or when forced)."""

    try:
        goal_id = _goal_id(goal)
        todo_id = _checked_id(todo_id, field="todo id")
        repos = _select_repos(goal, repo_names)
    except WorkspaceError as exc:
        return _error_payload("cleanup", goal, todo_id, exc, dry_run)
    payload = _envelope("cleanup", goal_id, todo_id, dry_run=dry_run)
    payload["force"] = force
    root = todo_workspace_root(runtime_root, goal_id, todo_id)
    results = []
    with _goal_lock(runtime_root, goal_id):
        for repo in repos:
            try:
                results.append(_cleanup_one(repo, goal_id, todo_id, root, force=force, dry_run=dry_run))
            except WorkspaceError as exc:
                results.append({"name": repo["name"], "ok": False, **exc.as_dict()})
        if not dry_run and root.is_dir() and not any(root.iterdir()):
            root.rmdir()
    payload["ok"] = all(item["ok"] for item in results)
    payload["repos"] = results
    return payload


def _cleanup_one(
    repo: Mapping[str, Any], goal_id: str, todo_id: str, root: Path, *, force: bool, dry_run: bool
) -> dict[str, Any]:
    ctx = _resolve_repo(repo, goal_id=goal_id)
    repo_dir = ctx["path"]
    branch = todo_branch(goal_id, todo_id)
    path = root / ctx["name"]
    result: dict[str, Any] = {"name": ctx["name"], "path": str(path), "branch": branch, **_ctx_fields(ctx)}
    worktrees = _list_worktrees(repo_dir)
    worktree = _worktree_at(worktrees, path)
    if worktree is not None and worktree.get("branch") not in (f"refs/heads/{branch}", None):
        return {**result, "ok": False, "action": "refused", "error_code": "foreign_worktree",
                "reason": f"{path} is on {worktree.get('branch')}, not {branch}"}
    if worktree is None and path.exists():
        return {**result, "ok": False, "action": "refused", "error_code": "path_not_a_worktree",
                "reason": f"{path} exists but is not a worktree of {repo_dir}; left untouched"}
    holder = _worktree_on_branch(worktrees, branch)
    if holder is not None and not _same_path(holder["path"], path):
        return {**result, "ok": False, "action": "refused", "error_code": "branch_checked_out_elsewhere",
                "reason": f"{branch} is checked out at {holder['path']}"}
    source_sha = _rev(repo_dir, f"refs/heads/{branch}")
    target_sha = _rev(repo_dir, f"refs/heads/{ctx['target_branch']}")
    merged = bool(source_sha and target_sha and _is_ancestor(repo_dir, source_sha, target_sha))
    live_worktree = worktree is not None and path.is_dir()
    dirty = _dirty_paths(path) if live_worktree else []
    result.update({"merged": merged, "dirty": bool(dirty), "branch_exists": source_sha is not None,
                   "worktree_exists": worktree is not None})
    if worktree is None and source_sha is None:
        return {**result, "ok": True, "action": "nothing_to_clean"}
    if not force and source_sha is not None and not merged:
        return {**result, "ok": False, "action": "skipped", "error_code": "not_merged",
                "reason": f"{branch} is not merged into {ctx['target_branch']}; pass --force to discard it"}
    if not force and dirty:
        return {**result, "ok": False, "action": "skipped", "error_code": "worktree_dirty",
                "reason": f"{path} has uncommitted changes; pass --force to discard them"}
    if dry_run:
        return {**result, "ok": True, "action": "would_remove"}
    if worktree is not None:
        if live_worktree:
            _git(repo_dir, "worktree", "remove", *(["--force"] if force or dirty else []), str(path))
        else:
            _drop_stale_registration(repo_dir, path)
    if source_sha is not None:
        _git(repo_dir, "branch", "-D", branch)
    return {**result, "ok": True, "action": "removed"}
