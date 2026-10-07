"""Push a goal's merged work to its remotes through a user gate (fork gap G8).

Design decision 18 makes pushing to a remote a user gate. Once a role_v1
goal's work is merged, LoopX (the dispatcher, like its other system gates)
opens one ``push_request`` user gate per goal; the orchestrator or the owner
can also ask for one (``loopx goal request-push``). The gate lists, per repo,
the merge target branch, the remote, the commit range and a short log.

* **approve** pushes each repo's merge target with a plain ``git push
  <remote> <branch>`` (never force), exactly the commits the gate showed. The
  result per repo is recorded as a ``push_result`` event. A failed push
  leaves a follow-up ``push_request`` gate carrying the error; approving it
  retries.
* **reject** and **cancel** push nothing. The decision is recorded, and the
  gate is not reopened until new merges move a merge target.

Safety: nothing is pushed unless the gate todo is closed with ``approve`` in
the goal state. Only the configured merge target is pushed (the LoopX task
branch ``loopx-task/<goal>``, or the default branch when the goal's
``merge_target`` is ``main``). Repos without a remote are skipped with a note.

The durable push state lives under the runtime root::

    goals/<goal>/push/state.json   declined heads, last results
    goals/<goal>/gates/index.json  kind=push_request entries (push_repos, push_outcome)
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .control_plane.todos.contract import TODO_UNFINISHED_STATUS_VALUES
from .file_lock import exclusive_file_lock
from .gate_threads import GATE_KIND_PUSH_REQUEST, mark_gate_closed, read_gate_index, register_gate_kind
from .history import validate_goal_id_path_segment
from .registry import atomic_write_json

PUSH_REQUEST_SCHEMA_VERSION = "loopx_push_request_v0"
PUSH_GATE_TEXT_PREFIX = "Push request: "
# Bounded gate content: log lines per repo and the kept tail of a push error.
PUSH_LOG_LINE_LIMIT = 10
PUSH_ERROR_TAIL_CHARS = 600
PUSH_GIT_TIMEOUT_SECONDS = 120
PUSH_COMMAND_TIMEOUT_SECONDS = 300
# Why a push gate opened.
PUSH_REASON_ALL_MERGED = "all_merged"
PUSH_REASON_REQUESTED = "requested"
PUSH_REASON_RETRY = "push_failed"
# Branch namespaces LoopX owns; any other branch must be the configured target.
PUSH_OWNED_BRANCH_PREFIXES = ("loopx-task/", "loopx/")
# Plan statuses of a local-only repo: skipped, never pushed, never pending.
PUSH_STATUS_NO_REMOTE = "no_remote"
PUSH_STATUS_NOT_A_GIT_REPO = "not_a_git_repo"
PUSH_STATUS_NO_COMMITS = "no_commits"
PUSH_LOCAL_ONLY_STATUSES = frozenset({PUSH_STATUS_NO_REMOTE, PUSH_STATUS_NOT_A_GIT_REPO, PUSH_STATUS_NO_COMMITS})

_ABSOLUTE_PATH = re.compile(r"(?:file://)?(?<![\w.~-])/[^\s'\"]+")


# --- storage -------------------------------------------------------------------


def push_dir(runtime_root: Path, goal_id: str) -> Path:
    return Path(runtime_root).expanduser() / "goals" / validate_goal_id_path_segment(goal_id) / "push"


def _state_path(runtime_root: Path, goal_id: str) -> Path:
    return push_dir(runtime_root, goal_id) / "state.json"


def read_push_state(runtime_root: Path, goal_id: str) -> dict[str, Any]:
    import json

    path = _state_path(runtime_root, goal_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _write_push_state(runtime_root: Path, goal_id: str, update: Mapping[str, Any]) -> None:
    path = _state_path(runtime_root, goal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {**read_push_state(runtime_root, goal_id), **dict(update),
                             "schema_version": PUSH_REQUEST_SCHEMA_VERSION})


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _event(runtime_root: Path, goal_id: str, event_kind: str, *, todo_id: str | None, status: str,
           details: Mapping[str, Any]) -> None:
    from .rollout_event_log import append_rollout_event, build_rollout_event, rollout_event_log_path

    try:
        try:
            event = build_rollout_event(goal_id=goal_id, event_kind=event_kind, todo_id=todo_id, status=status,
                                        details=dict(details))
        except ValueError:
            # The public-safety filter refused a value (for example git's
            # error text): keep the event, withhold the text.
            safe = {key: value for key, value in details.items() if key != "error_tail"}
            event = build_rollout_event(goal_id=goal_id, event_kind=event_kind, todo_id=todo_id, status=status,
                                        details={**safe, "error_tail": "withheld"})
        append_rollout_event(rollout_event_log_path(runtime_root, goal_id), event)
    except (OSError, ValueError):
        pass  # the event log is an audit trail; the push state already records the result


def redact_paths(text: str) -> str:
    """Replace absolute paths and file URLs (local remotes, repo dirs) with ``<path>``."""

    return _ABSOLUTE_PATH.sub("<path>", str(text or ""))


# --- git -----------------------------------------------------------------------------


def _git(cwd: str | Path, *args: str, timeout: float = PUSH_GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["git", *args], 124, "", f"git {args[0]} timed out after {timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(["git", *args], 127, "", f"cannot run git: {exc}")


def _out(cwd: str | Path, *args: str) -> str | None:
    completed = _git(cwd, *args)
    return completed.stdout.strip() if completed.returncode == 0 else None


def _rev(cwd: str | Path, ref: str) -> str | None:
    return _out(cwd, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}") or None


def _remote_for(repo_dir: str | Path, branch: str) -> str | None:
    """The branch's configured upstream remote, else ``origin`` when it exists."""

    remotes = set((_out(repo_dir, "remote") or "").split())
    upstream = _out(repo_dir, "config", "--get", f"branch.{branch}.remote")
    if upstream and upstream in remotes:
        return upstream
    return "origin" if "origin" in remotes else None


def _goal_trailer_pattern(goal_id: str) -> str:
    # Goal ids are [A-Za-z0-9._-]; only the dot is special in an ERE.
    return f"^LoopX-Goal: {goal_id.replace('.', '[.]')}$"


def repo_push_plan(goal: Mapping[str, Any], repo: Mapping[str, Any], goal_id: str) -> dict[str, Any]:
    """What pushing one repo's merge target would send, without touching the network.

    ``status`` is ``ready`` (commits to push), ``up_to_date``, a local-only
    status (skipped: ``no_remote``; for the implicit repo of a goal created
    without ``--repo``, ``not_a_git_repo`` or ``no_commits`` when its project
    directory is no git repository or has no commit yet), ``no_branch`` or
    ``error``.
    """

    from .workspace.git_workspace import WorkspaceError, _resolve_repo

    name = str(repo.get("name") or "")
    try:
        resolved = _resolve_repo(repo, goal_id=goal_id)
    except WorkspaceError as exc:
        # A goal created without --repo names its project directory as the
        # implicit repo; a plain directory, or a repo with no commit yet (an
        # unborn HEAD), has nothing to push. A declared repo stays an error.
        if repo.get("legacy") and exc.code == "not_a_git_repo":
            return {"name": name, "status": PUSH_STATUS_NOT_A_GIT_REPO,
                    "note": "the project directory is not a git repository; nothing to push"}
        if repo.get("legacy") and exc.code == "default_branch_unresolved" and _rev(str(repo["path"]), "HEAD") is None:
            return {"name": name, "status": PUSH_STATUS_NO_COMMITS,
                    "note": "the project directory has no commit yet; nothing to push"}
        return {"name": name, "status": "error", "error": exc.reason}
    path = resolved["path"]
    branch = str(resolved["target_branch"])
    plan: dict[str, Any] = {"name": name, "branch": branch, "merge_target": resolved["merge_target"]}
    head = _rev(path, f"refs/heads/{branch}")
    if head is None:
        return {**plan, "status": "no_branch", "note": f"merge target {branch} does not exist yet"}
    plan["head"] = head
    remote = _remote_for(path, branch)
    if remote is None:
        return {**plan, "status": PUSH_STATUS_NO_REMOTE, "remote": None,
                "note": "no remote configured (local-only repo); not pushed"}
    plan["remote"] = remote
    exclude = ("--not", f"--remotes={remote}")
    unpushed = int(_out(path, "rev-list", "--count", head, *exclude) or 0)
    goal_merges = int(_out(path, "rev-list", "--count", "-E", f"--grep={_goal_trailer_pattern(goal_id)}",
                           head, *exclude) or 0)
    tracking = _rev(path, f"refs/remotes/{remote}/{branch}")
    log = (_out(path, "log", "--oneline", "--no-decorate", f"-n{PUSH_LOG_LINE_LIMIT}", head, *exclude) or "")
    plan.update(
        tracking=tracking,
        commit_range=f"{tracking[:12] if tracking else '(new branch)'}..{head[:12]}",
        unpushed_commits=unpushed,
        goal_merge_commits=goal_merges,
        log=[line for line in log.splitlines() if line.strip()][:PUSH_LOG_LINE_LIMIT],
        status="ready" if unpushed else "up_to_date",
    )
    return plan


def goal_push_plan(goal: Mapping[str, Any], goal_id: str) -> list[dict[str, Any]]:
    from .workspace.repos import goal_repos

    try:
        repos = goal_repos(goal)
    except ValueError as exc:
        return [{"name": "", "status": "error", "error": str(exc)}]
    return [repo_push_plan(goal, repo, goal_id) for repo in repos]


# --- opening the gate ------------------------------------------------------------------


def _pending_agent_todos(registry_path: Path, goal_id: str, runtime_root_arg: str | None) -> list[str]:
    from .todos import list_goal_todos

    listed = list_goal_todos(registry_path=registry_path, goal_id=goal_id, role="agent",
                             runtime_root_arg=runtime_root_arg)
    return [
        str(row.get("todo_id")) for row in listed.get("todos") or []
        if isinstance(row, Mapping) and str(row.get("status") or "") in TODO_UNFINISHED_STATUS_VALUES
    ]


def _open_gate_ids(registry_path: Path, goal_id: str, runtime_root_arg: str | None) -> set[str]:
    from .todos import list_goal_todos

    listed = list_goal_todos(registry_path=registry_path, goal_id=goal_id, role="user", status="open",
                             runtime_root_arg=runtime_root_arg)
    return {str(row.get("todo_id")) for row in listed.get("todos") or [] if isinstance(row, Mapping)}


def open_push_gate_id(runtime_root: Path, goal_id: str, open_user_todo_ids: set[str]) -> str | None:
    """The goal's open ``push_request`` gate, if any (at most one per goal)."""

    for gate_id, entry in read_gate_index(runtime_root, goal_id)["gates"].items():
        if (isinstance(entry, Mapping) and entry.get("kind") == GATE_KIND_PUSH_REQUEST
                and not entry.get("closed") and gate_id in open_user_todo_ids):
            return str(gate_id)
    return None


def _repo_summary(repo: Mapping[str, Any]) -> str:
    if repo.get("status") == "ready":
        return (f"{repo['name']}: {repo['branch']} -> {repo['remote']} "
                f"({repo['unpushed_commits']} commit(s), {repo['commit_range']})")
    return f"{repo.get('name')}: skipped, {repo.get('note') or repo.get('error') or repo.get('status')}"


def _gate_text(goal_id: str, repos: Sequence[Mapping[str, Any]], reason: str,
               previous_errors: Sequence[Mapping[str, Any]]) -> str:
    lead = (f"push again after a failed push of {goal_id}'s merged work? "
            if reason == PUSH_REASON_RETRY else f"push {goal_id}'s merged work to its remotes? ")
    parts = [_repo_summary(repo) for repo in repos
             if repo.get("status") == "ready" or repo.get("status") in PUSH_LOCAL_ONLY_STATUSES]
    text = PUSH_GATE_TEXT_PREFIX + lead + "; ".join(parts) + "."
    if previous_errors:
        text += " Last push failed: " + "; ".join(
            f"{item.get('name')}: {redact_paths(str(item.get('error_tail') or ''))[-200:]}" for item in previous_errors
        ) + "."
    return text + (
        " Approve pushes each merge target (plain git push, never force); reject keeps it local until new "
        f"merges arrive; cancel dismisses. `loopx gate show --goal-id {goal_id} --todo-id <this gate>` lists "
        f"the commits; `loopx gate resolve --goal-id {goal_id} --todo-id <this gate> --decision "
        "approve|reject|cancel`."
    )


def request_push(
    *, registry_path: Path, goal_id: str, runtime_root_arg: str | None = None,
    reason: str = PUSH_REASON_REQUESTED, requested_by: str | None = None,
    require_all_merged: bool = False, respect_declined: bool = True,
    previous_errors: Sequence[Mapping[str, Any]] = (), dry_run: bool = False,
) -> dict[str, Any]:
    """Open the goal's ``push_request`` gate when its merge targets have commits to push.

    Idempotent: an open push gate is returned instead of a second one. With
    ``require_all_merged`` (the dispatcher) no agent todo may be unfinished
    (open, in review, blocked or deferred), so a partial goal is never offered;
    an explicit request (the CLI) does not wait for them. With
    ``respect_declined`` a gate the user rejected or cancelled is not reopened
    until a merge target moves.
    """

    from .agent_registry import load_goal_from_registry, orchestrator_agent_for_goal
    from .paths import effective_runtime_root
    from .todo_acceptance import goal_uses_role_v1

    goal = load_goal_from_registry(Path(registry_path), goal_id)
    if goal is None:
        raise ValueError(f"goal {goal_id!r} is not registered")
    base = {"ok": True, "schema_version": PUSH_REQUEST_SCHEMA_VERSION, "goal_id": goal_id, "opened": False,
            "dry_run": dry_run}
    if not goal_uses_role_v1(goal):
        return {**base, "ok": False, "reason": "not_role_v1",
                "error": "push gates apply to role_v1 goals (design decision 18)"}
    runtime_root = effective_runtime_root(Path(registry_path), runtime_root_arg)
    lock = push_dir(runtime_root, goal_id) / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(lock):
        open_ids = _open_gate_ids(Path(registry_path), goal_id, runtime_root_arg)
        existing = open_push_gate_id(runtime_root, goal_id, open_ids)
        if existing:
            return {**base, "reason": "push_gate_open", "gate_todo_id": existing}
        if require_all_merged:
            pending = _pending_agent_todos(Path(registry_path), goal_id, runtime_root_arg)
            if pending:
                return {**base, "reason": "todos_pending", "pending_todo_ids": pending[:20]}
        repos = goal_push_plan(goal, goal_id)
        ready = [repo for repo in repos if repo.get("status") == "ready"]
        if require_all_merged:
            # The dispatcher only offers this goal's own merges, never other
            # unpushed commits that happen to sit on a shared target branch.
            ready = [repo for repo in ready if int(repo.get("goal_merge_commits") or 0) > 0]
        if not ready:
            return {**base, "reason": "nothing_to_push", "repos": repos}
        heads = {str(repo["name"]): str(repo["head"]) for repo in ready}
        declined = read_push_state(runtime_root, goal_id).get("declined") or {}
        if respect_declined and declined.get("heads") == heads:
            return {**base, "reason": "declined_until_new_merges", "declined_gate": declined.get("gate_todo_id"),
                    "repos": repos}
        shown = ready + [repo for repo in repos if repo.get("status") in PUSH_LOCAL_ONLY_STATUSES]
        text = _gate_text(goal_id, shown, reason, previous_errors)
        if dry_run:
            return {**base, "reason": reason, "gate_text": text, "repos": shown}
        from .todos import add_goal_todo

        orchestrator = orchestrator_agent_for_goal(dict(goal))
        # The owner path (no agent id): LoopX opens this gate, like the
        # dispatcher's re-login gate (decision 17); it holds the orchestrator.
        gate = add_goal_todo(
            registry_path=Path(registry_path), goal_id=goal_id, role="user", text=text,
            task_class="user_gate", blocks_agent=orchestrator,
            note="Opened by LoopX: the goal's merged work is ready to push (decision 18).",
            runtime_root_arg=runtime_root_arg,
        )
        gate_id = str(gate.get("todo_id") or "")
        register_gate_kind(runtime_root, goal_id, gate_id, kind=GATE_KIND_PUSH_REQUEST, extra={
            "push_reason": reason, "push_repos": shown,
            **({"requested_by": requested_by} if requested_by else {}),
            **({"previous_errors": [dict(item) for item in previous_errors]} if previous_errors else {}),
        })
    _event(runtime_root, goal_id, "push_requested", todo_id=gate_id, status=reason, details={
        "gate_id": gate_id, "repos": ",".join(sorted(heads)), "requested_by": requested_by or "loopx",
    })
    return {**base, "opened": True, "reason": reason, "gate_todo_id": gate_id, "gate_text": text, "repos": shown}


# --- resolving the gate --------------------------------------------------------------------


def push_gate_entry(runtime_root: Path, goal_id: str, gate_todo_id: str) -> dict[str, Any] | None:
    entry = read_gate_index(runtime_root, goal_id)["gates"].get(str(gate_todo_id))
    if isinstance(entry, Mapping) and entry.get("kind") == GATE_KIND_PUSH_REQUEST:
        return dict(entry)
    return None


def _approved_in_state(registry_path: Path, goal_id: str, gate_todo_id: str, runtime_root_arg: str | None) -> bool:
    from .todos import list_goal_todos

    listed = list_goal_todos(registry_path=registry_path, goal_id=goal_id, todo_id=gate_todo_id,
                             runtime_root_arg=runtime_root_arg)
    for row in listed.get("todos") or []:
        if isinstance(row, Mapping) and row.get("todo_id") == gate_todo_id:
            return row.get("status") == "done" and row.get("decision_outcome") == "approve"
    return False


def _push_allowed(resolved: Mapping[str, Any], branch: str) -> str | None:
    """Why pushing ``branch`` is refused, or None when it is the configured merge target."""

    if branch != resolved.get("target_branch"):
        return f"{branch} is no longer the configured merge target ({resolved.get('target_branch')})"
    if branch.startswith(PUSH_OWNED_BRANCH_PREFIXES):
        return None
    if resolved.get("merge_target") == "main" and branch == resolved.get("default_branch"):
        return None
    return f"{branch} is neither a LoopX branch nor the configured merge target"


def _run_on_push_command(goal: Mapping[str, Any], repo_dir: str) -> dict[str, Any] | None:
    raw = goal.get("on_push_command")
    if not raw:
        return None
    argv = shlex.split(raw) if isinstance(raw, str) else [str(item) for item in raw]
    if not argv:
        return None
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        completed = subprocess.run(argv, cwd=repo_dir, env=env, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=PUSH_COMMAND_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "returncode": None, "error_tail": redact_paths(str(exc))[-PUSH_ERROR_TAIL_CHARS:]}
    tail = (completed.stderr or completed.stdout or "").strip()
    return {"ok": completed.returncode == 0, "returncode": completed.returncode,
            "output_tail": redact_paths(tail)[-PUSH_ERROR_TAIL_CHARS:]}


def _push_repo(goal: Mapping[str, Any], goal_id: str, approved: Mapping[str, Any]) -> dict[str, Any]:
    from .workspace.git_workspace import WorkspaceError, _resolve_repo
    from .workspace.repos import goal_repos

    name = str(approved.get("name") or "")
    result: dict[str, Any] = {"name": name, "branch": approved.get("branch"), "remote": approved.get("remote")}
    repo = next((item for item in goal_repos(goal) if item.get("name") == name), None)
    if repo is None:
        return {**result, "status": "error", "error_tail": "the repo is no longer declared by the goal"}
    try:
        resolved = _resolve_repo(repo, goal_id=goal_id)
    except WorkspaceError as exc:
        return {**result, "status": "error", "error_tail": redact_paths(exc.reason)}
    branch = str(approved.get("branch") or "")
    refused = _push_allowed(resolved, branch)
    if refused:
        return {**result, "status": "refused", "error_tail": refused}
    remote = str(approved.get("remote") or "")
    path = resolved["path"]
    head = _rev(path, f"refs/heads/{branch}")
    approved_head = str(approved.get("head") or "")
    if head != approved_head:
        # New merges arrived after the gate opened; they were not approved.
        # The branch stays unpushed and the dispatcher offers it again.
        return {**result, "status": "head_moved", "head": head, "approved_head": approved_head}
    tracking = _rev(path, f"refs/remotes/{remote}/{branch}")
    if tracking == head:
        return {**result, "status": "already_pushed", "head": head}
    completed = _git(path, "push", remote, branch)
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip()
        return {**result, "status": "error", "returncode": completed.returncode,
                "error_tail": redact_paths(tail)[-PUSH_ERROR_TAIL_CHARS:]}
    pushed = {**result, "status": "ok", "head": head}
    hook = _run_on_push_command(goal, path)
    if hook is not None:
        pushed["on_push_command"] = hook
    return pushed


def settle_push_gate(
    *, registry_path: Path, runtime_root: Path, goal_id: str, gate_todo_id: str, decision: str | None,
    runtime_root_arg: str | None = None,
) -> dict[str, Any] | None:
    """After a ``push_request`` gate closed: push (approve) or record the decline.

    Returns None for other gates. Settling the same gate again replays its
    recorded outcome and pushes nothing.
    """

    from .agent_registry import load_goal_from_registry

    entry = push_gate_entry(runtime_root, goal_id, gate_todo_id)
    if entry is None:
        return None
    if isinstance(entry.get("push_outcome"), Mapping):
        return {"payload_key": "push", **dict(entry["push_outcome"]), "replayed": True}
    mark_gate_closed(runtime_root, goal_id, gate_todo_id, decision=decision)
    approved_repos = [repo for repo in entry.get("push_repos") or [] if repo.get("status") == "ready"]
    outcome: dict[str, Any] = {"ok": True, "gate_todo_id": gate_todo_id, "decision": decision, "pushed": False,
                               "repos": []}
    if decision != "approve":
        heads = {str(repo["name"]): str(repo["head"]) for repo in approved_repos}
        _write_push_state(runtime_root, goal_id, {"declined": {
            "gate_todo_id": gate_todo_id, "decision": decision, "heads": heads, "at": _now()}})
        _event(runtime_root, goal_id, "push_declined", todo_id=gate_todo_id, status=str(decision),
               details={"gate_id": gate_todo_id, "repos": ",".join(sorted(heads))})
    elif not _approved_in_state(Path(registry_path), goal_id, gate_todo_id, runtime_root_arg):
        # Never push on anything but an approve decision recorded in state.
        outcome.update(ok=False, error="the gate's approve decision is not recorded in the goal state")
    else:
        goal = load_goal_from_registry(Path(registry_path), goal_id) or {}
        results = [_push_repo(goal, goal_id, repo) for repo in approved_repos]
        skipped = [
            {"name": repo.get("name"), "status": "skipped", "note": repo.get("note")}
            for repo in entry.get("push_repos") or [] if repo.get("status") in PUSH_LOCAL_ONLY_STATUSES
        ]
        outcome["repos"] = results + skipped
        outcome["pushed"] = any(item["status"] == "ok" for item in results)
        failures = [item for item in results if item["status"] in {"error", "refused"}]
        outcome["ok"] = not failures
        for item in results:
            _event(runtime_root, goal_id, "push_result", todo_id=gate_todo_id, status=item["status"], details={
                "gate_id": gate_todo_id, "repo": item.get("name"), "branch": item.get("branch"),
                "remote": item.get("remote"), "head": item.get("head"),
                **({"error_tail": item["error_tail"]} if item.get("error_tail") else {}),
                **({"on_push_command_ok": item["on_push_command"]["ok"]} if item.get("on_push_command") else {}),
            })
        _write_push_state(runtime_root, goal_id, {"last_push": {
            "gate_todo_id": gate_todo_id, "at": _now(),
            "repos": [{key: item.get(key) for key in ("name", "status", "head")} for item in results]}})
        if failures:
            follow_up = request_push(
                registry_path=Path(registry_path), goal_id=goal_id, runtime_root_arg=runtime_root_arg,
                reason=PUSH_REASON_RETRY, requested_by="loopx", respect_declined=False,
                previous_errors=[{key: item.get(key) for key in ("name", "status", "error_tail")}
                                 for item in failures],
            )
            outcome["follow_up_gate_todo_id"] = follow_up.get("gate_todo_id")
    mark_gate_closed(runtime_root, goal_id, gate_todo_id, decision=decision, extra={"push_outcome": outcome})
    return {"payload_key": "push", **outcome}


# --- Markdown (``gate show``, ``gate resolve`` and ``goal request-push``) ---------------------------


def _rows(value: Any) -> list[Mapping[str, Any]]:
    """The mapping entries of a list read from the gate index; anything else is skipped."""

    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _error_text(text: Any) -> str:
    """Error or note text on one line, with local paths redacted."""

    return redact_paths(" ".join(str(text or "").split()))


def _repo_markdown(repo: Mapping[str, Any]) -> list[str]:
    """One repo of a push gate: branch -> remote, commit count and range, then its bounded log."""

    if repo.get("status") != "ready":
        detail = repo.get("note") or repo.get("error") or str(repo.get("status") or "").replace("_", " ")
        return [f"- `{repo.get('name')}`: skipped, {_error_text(detail)}"]
    log = repo.get("log")
    return [
        f"- `{repo.get('name')}`: {repo.get('branch')} -> {repo.get('remote')}, "
        f"{repo.get('unpushed_commits')} commit(s), {repo.get('commit_range')}",
        *(f"  - {line}" for line in (log if isinstance(log, list) else [])),
    ]


def push_outcome_markdown(outcome: Mapping[str, Any]) -> list[str]:
    """What settling a push gate did, per repo (the ``push`` payload or the gate's ``push_outcome``)."""

    decision = outcome.get("decision")
    if decision != "approve":
        state = f"not pushed ({decision})"
    elif not outcome.get("ok"):
        state = "failed"
    else:
        state = "pushed" if outcome.get("pushed") else "nothing pushed"
    lines = [f"- push: {state}"]
    if outcome.get("error"):
        lines.append(f"- error: {_error_text(outcome['error'])}")
    for item in _rows(outcome.get("repos")):
        status = item.get("status") if isinstance(item.get("status"), str) else "unknown"
        if status in {"ok", "already_pushed"}:
            detail = f"{status}, {item.get('branch')} -> {item.get('remote')}"
        elif status == "skipped":
            detail = f"skipped, {_error_text(item.get('note'))}"
        elif status == "head_moved":
            detail = "not pushed, new merges arrived after the gate opened"
        else:
            detail = f"{status}: {_error_text(item.get('error_tail'))}"
        lines.append(f"- `{item.get('name')}`: {detail}")
    if outcome.get("follow_up_gate_todo_id"):
        lines.append(f"- follow-up gate: `{outcome['follow_up_gate_todo_id']}` (approve it to push again)")
    return lines


def render_push_gate_markdown(view: Mapping[str, Any]) -> list[str]:
    """The ``gate show`` section of a push_request gate: the repos it offers and, once closed, its result."""

    lines = ["", f"## Push ({view.get('push_reason') or PUSH_REASON_REQUESTED})", ""]
    for repo in _rows(view.get("push_repos")):
        lines += _repo_markdown(repo)
    for item in _rows(view.get("previous_errors")):
        lines.append(f"- previous push failed: `{item.get('name')}`: {_error_text(item.get('error_tail'))}")
    if isinstance(view.get("push_outcome"), Mapping):
        lines += push_outcome_markdown(view["push_outcome"])
    return lines


def render_push_request_markdown(payload: Mapping[str, Any]) -> str:
    """``loopx goal request-push``: the opened gate and its repos, or why none opened."""

    if not payload.get("ok"):
        return f"push request: error: {_error_text(payload.get('error'))}\n"
    goal_id, gate_id, reason = payload.get("goal_id"), payload.get("gate_todo_id"), payload.get("reason")
    if payload.get("opened"):
        lines = [f"Opened push gate `{gate_id}` for {goal_id} ({reason})."]
    elif gate_id:
        lines = [f"Push gate `{gate_id}` is already open for {goal_id}."]
    elif payload.get("gate_text"):
        lines = [f"Would open a push gate for {goal_id} ({reason}); dry run, nothing opened."]
    else:
        lines = [f"No push gate opened for {goal_id}: {reason}."]
    if isinstance(payload.get("pending_todo_ids"), list) and payload["pending_todo_ids"]:
        lines.append("- unfinished todos: " + ", ".join(f"`{todo}`" for todo in payload["pending_todo_ids"]))
    if payload.get("declined_gate"):
        lines.append(f"- declined at gate `{payload['declined_gate']}`; new merges offer it again")
    for repo in _rows(payload.get("repos")):
        lines += _repo_markdown(repo)
    if gate_id:
        lines += ["", f"Review it with `loopx gate show --goal-id {goal_id} --todo-id {gate_id}`; decide with "
                      f"`loopx gate resolve --goal-id {goal_id} --todo-id {gate_id} --decision "
                      "approve|reject|cancel`."]
    return "\n".join(lines) + "\n"
