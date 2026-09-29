"""A deterministic goal_complete gate once a role_v1 goal's work is done (design decision 42).

Under role_v1 the goal's Next Action is written by whichever Turn settled
last, so it is no orchestrator obligation (decision 42 extends decision 39);
nothing then woke the orchestrator on a finished goal except that stale
text, and in the E2E pilot v1 it did, for a Fable Turn that opened a
"Goal complete: confirm closure" gate (gap N2). LoopX now opens that gate
itself, with no model Turn.

The goal's work is finished when:

* no agent todo is ``open``, ``in_review``, ``blocked`` or ``deferred``;
* no user gate is open (a pending push gate included);
* at least one agent todo that is not orchestrator work is done, and every
  done todo that requires acceptance carries an accept record (accepted and
  merged, decision 37); superseded todos never count;
* the push is resolved in every repo: pushed (or rejected/cancelled at the
  push gate, decision 38), or nothing to push (no remote, no merges, or no
  unpushed merges of this goal).

The dispatcher then opens one system user gate of kind ``goal_complete``,
like the push (G8) and budget (decision 41) gates. Its content is computed
without a model: per repo the merge target, the merged todo commits and the
push result; the accepted, rejected and superseded todo counts; the usage
report totals and per-role split; open follow-ups (open user todos that are
not gates).

One completion is one ``completion_key`` (the done agent todos and the merge
target heads). The gate index remembers it, so a replayed or restarted
dispatcher never opens a second gate for the same completion, and a
completion the owner left open is not offered again until new work finishes.

The owner resolves the gate with one of three options:

=====================  =========  =============================================
option                 decision   effect
=====================  =========  =============================================
``close_goal``         approve    stop the goal through the existing reversible
                                  lifecycle (``loopx goal-lifecycle``); the
                                  dispatcher no longer considers it until it is
                                  resumed
``add_work``           reject     the note (required) becomes one orchestrator
                                  action todo "User follow-up: <note>", which
                                  launches an ordinary orchestrator Turn
``leave_open``         cancel     nothing changes; no further goal_complete
                                  gate until new work finishes
=====================  =========  =============================================

``approve`` without an option means ``close_goal``, ``reject`` means
``add_work`` and ``cancel`` means ``leave_open``. The gate closes through the
ordinary gate decision path (``loopx gate resolve``, ``loopx todo complete
--role user --decision-outcome`` or the dashboard ``gate.resolve``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .control_plane.todos.contract import TODO_UNFINISHED_STATUS_VALUES
from .file_lock import exclusive_file_lock
from .gate_threads import GATE_KIND_GOAL_COMPLETE, read_gate_index, register_gate_kind, run_gate_settlement
from .history import validate_goal_id_path_segment

GOAL_COMPLETE_SCHEMA_VERSION = "loopx_goal_complete_gate_v0"
GOAL_COMPLETE_GATE_TEXT_PREFIX = "Goal complete: "
GOAL_COMPLETE_OPTION_CLOSE = "close_goal"
GOAL_COMPLETE_OPTION_ADD_WORK = "add_work"
GOAL_COMPLETE_OPTION_LEAVE_OPEN = "leave_open"
GOAL_COMPLETE_GATE_OPTIONS = (
    GOAL_COMPLETE_OPTION_CLOSE, GOAL_COMPLETE_OPTION_ADD_WORK, GOAL_COMPLETE_OPTION_LEAVE_OPEN,
)
GOAL_COMPLETE_OPTION_DECISIONS = {
    GOAL_COMPLETE_OPTION_CLOSE: "approve",
    GOAL_COMPLETE_OPTION_ADD_WORK: "reject",
    GOAL_COMPLETE_OPTION_LEAVE_OPEN: "cancel",
}
_GOAL_COMPLETE_DEFAULT_OPTION = {
    None: GOAL_COMPLETE_OPTION_CLOSE,
    "approve": GOAL_COMPLETE_OPTION_CLOSE,
    "reject": GOAL_COMPLETE_OPTION_ADD_WORK,
    "cancel": GOAL_COMPLETE_OPTION_LEAVE_OPEN,
}
# Dispatcher skip reason for a goal the owner closed at its goal_complete gate.
GOAL_COMPLETE_HOLD_CLOSED = "goal_closed_by_owner"
# The follow-up todo's text names its gate, so a replayed settle finds it again.
GOAL_COMPLETE_FOLLOW_UP_LABEL = "User follow-up: "
# Why a goal is not complete yet (``goal_completion_snapshot`` reasons).
GOAL_COMPLETE_WAIT_TODOS = "todos_pending"
GOAL_COMPLETE_WAIT_GATE = "user_gate_open"
GOAL_COMPLETE_WAIT_PUSH = "push_pending"
GOAL_COMPLETE_WAIT_ACCEPTANCE = "acceptance_missing"
GOAL_COMPLETE_NO_WORK = "no_finished_work"
# Bounded gate content.
_MERGED_COMMIT_LIMIT = 20
_FOLLOW_UP_LIMIT = 8
_ROLE_ROWS = 8
_FOLLOW_UP_NOTE_CHARS = 400
_TODO_TOKEN = re.compile(r"todo_[A-Za-z0-9_-]+")


# --- storage -------------------------------------------------------------------


def _lock_path(runtime_root: Path, goal_id: str) -> Path:
    return (Path(runtime_root).expanduser() / "goals" / validate_goal_id_path_segment(goal_id)
            / "goal-complete" / ".lock")


def goal_complete_gate_entries(runtime_root: Path, goal_id: str) -> dict[str, dict[str, Any]]:
    return {
        str(gate_id): dict(entry)
        for gate_id, entry in read_gate_index(runtime_root, goal_id)["gates"].items()
        if isinstance(entry, Mapping) and entry.get("kind") == GATE_KIND_GOAL_COMPLETE
    }


def goal_complete_gate_entry(runtime_root: Path, goal_id: str, gate_todo_id: str) -> dict[str, Any] | None:
    return goal_complete_gate_entries(runtime_root, goal_id).get(str(gate_todo_id))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _event(runtime_root: Path, goal_id: str, event_kind: str, *, todo_id: str, status: str,
           details: Mapping[str, Any]) -> None:
    from .rollout_event_log import append_rollout_event, build_rollout_event, rollout_event_log_path

    try:
        append_rollout_event(
            rollout_event_log_path(runtime_root, goal_id),
            build_rollout_event(goal_id=goal_id, event_kind=event_kind, todo_id=todo_id, status=status,
                                details=dict(details)),
        )
    except (OSError, ValueError):
        pass  # the event log is an audit trail; the gate index already records it


# --- the completion snapshot -----------------------------------------------------------


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _is_orchestrator_work(goal: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    from .agent_registry import orchestrator_agent_for_goal

    orchestrator = orchestrator_agent_for_goal(dict(goal))
    return (str(row.get("required_role") or "") == "orchestrator"
            or bool(orchestrator and row.get("claimed_by") == orchestrator))


def _merged_todo_commits(repo_dir: str, head: str, goal_id: str) -> list[dict[str, str]]:
    from .push_requests import _goal_trailer_pattern, _out

    log = _out(repo_dir, "log", "--no-decorate", "-E", f"--grep={_goal_trailer_pattern(goal_id)}",
               f"-n{_MERGED_COMMIT_LIMIT}", "--format=%h%x09%s", head) or ""
    commits = []
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        if not sha.strip():
            continue
        todo = _TODO_TOKEN.search(subject)
        commits.append({"sha": sha.strip(), "todo_id": todo.group(0) if todo else ""})
    return commits


def _repo_completion(goal: Mapping[str, Any], repo: Mapping[str, Any], goal_id: str,
                     push_state: Mapping[str, Any]) -> dict[str, Any]:
    """One repo's merge target, merged todo commits and push result (``pending`` blocks)."""

    from .push_requests import repo_push_plan, redact_paths
    from .workspace.git_workspace import WorkspaceError, _resolve_repo

    plan = repo_push_plan(goal, repo, goal_id)
    name = str(plan.get("name") or repo.get("name") or "")
    row: dict[str, Any] = {"name": name, "branch": plan.get("branch"), "remote": plan.get("remote"),
                           "head": plan.get("head")}
    status = plan.get("status")
    if status == "error":
        return {**row, "push": "pending", "error": redact_paths(str(plan.get("error") or ""))[:200]}
    if plan.get("head"):
        try:
            repo_dir = _resolve_repo(repo, goal_id=goal_id)["path"]
            row["merged_todo_commits"] = _merged_todo_commits(repo_dir, str(plan["head"]), goal_id)
        except WorkspaceError:
            row["merged_todo_commits"] = []
    head = str(plan.get("head") or "")
    last = {
        str(item.get("name")): item for item in (push_state.get("last_push") or {}).get("repos") or []
        if isinstance(item, Mapping)
    }
    declined = (push_state.get("declined") or {}).get("heads") or {}
    if status == "no_branch":
        return {**row, "push": "no_merges"}
    if status == "no_remote":
        return {**row, "push": "local_only"}
    if status == "up_to_date":
        pushed = last.get(name, {})
        was_pushed = pushed.get("status") in {"ok", "already_pushed"} and pushed.get("head") == head
        return {**row, "push": "pushed" if was_pushed else "up_to_date"}
    if int(plan.get("goal_merge_commits") or 0) == 0:
        return {**row, "push": "nothing_to_push", "unpushed_commits": plan.get("unpushed_commits")}
    if declined.get(name) == head:
        decision = (push_state.get("declined") or {}).get("decision") or "reject"
        return {**row, "push": "declined", "push_decision": decision,
                "unpushed_commits": plan.get("unpushed_commits")}
    return {**row, "push": "pending", "unpushed_commits": plan.get("unpushed_commits")}


def _usage(registry_path: Path, runtime_root: Path, goal_id: str) -> dict[str, Any]:
    from .usage_accounting.report import build_usage_report

    report = build_usage_report(registry_path=registry_path, runtime_root=runtime_root, goal_ids=[goal_id],
                                by="role")
    totals = report.get("totals") or {}
    keys = ("cost_usd", "cost_reported_usd", "cost_estimated_usd", "unpriced_turns", "turns", "failed_turns",
            "agent_hours", "accepted_todos", "cost_per_accepted_todo_usd",
            "cost_per_accepted_todo_excl_orchestrator_usd", "orchestrator_cost_usd")
    return {
        **{key: totals.get(key) for key in keys if key in totals},
        "by_role": [
            {"role": row.get("key"), "cost_usd": row.get("cost_usd"), "turns": row.get("turns"),
             "agent_hours": row.get("agent_hours")}
            for row in report.get("groups") or []
        ][:_ROLE_ROWS],
    }


def _completion_key(done_ids: Sequence[str], repos: Sequence[Mapping[str, Any]]) -> str:
    material = {"done": sorted(done_ids), "heads": {str(repo.get("name")): repo.get("head") for repo in repos}}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def goal_completion_snapshot(
    *, registry_path: Path, runtime_root: Path, goal_id: str, goal: Mapping[str, Any] | None = None,
    runtime_root_arg: str | None = None, ignore_gate_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Whether a role_v1 goal's work is finished, with the gate content when it is.

    ``complete`` is False with a ``reason`` while work, a user gate or a push
    is pending. Everything is read from the todo state, the plan
    supersessions, the repos and the usage ledger; no model is involved.
    """

    from .agent_registry import load_goal_from_registry
    from .control_plane.goals.activation import goal_is_stopped
    from .plan_dependencies import accepted_by, read_supersessions, supersession_map, todo_is_superseded
    from .push_requests import read_push_state
    from .todo_acceptance import delivery_requires_review, goal_uses_role_v1
    from .todos import list_goal_todos
    from .workspace.repos import goal_repos

    goal = goal if goal is not None else load_goal_from_registry(Path(registry_path), goal_id)
    base: dict[str, Any] = {"goal_id": goal_id, "complete": False}
    if not goal_uses_role_v1(goal):
        return {**base, "reason": "not_role_v1"}
    if goal_is_stopped(goal):
        return {**base, "reason": "goal_stopped"}
    rows = [
        row for row in list_goal_todos(registry_path=Path(registry_path), goal_id=goal_id,
                                       runtime_root_arg=runtime_root_arg or str(runtime_root)).get("todos") or []
        if isinstance(row, Mapping)
    ]
    agent_rows = [row for row in rows if str(row.get("role") or "agent") == "agent"]
    user_rows = [row for row in rows if str(row.get("role") or "") == "user"]
    pending = [str(row.get("todo_id")) for row in agent_rows
               if str(row.get("status") or "") in TODO_UNFINISHED_STATUS_VALUES]
    if pending:
        return {**base, "reason": GOAL_COMPLETE_WAIT_TODOS, "pending_todo_ids": pending[:20]}
    open_gates = [str(row.get("todo_id")) for row in user_rows
                  if row.get("status") in {"open", "blocked"} and row.get("task_class") == "user_gate"
                  and str(row.get("todo_id")) not in ignore_gate_ids]
    if open_gates:
        return {**base, "reason": GOAL_COMPLETE_WAIT_GATE, "open_gate_ids": open_gates[:20]}
    mapping = supersession_map(read_supersessions(runtime_root, goal_id))
    superseded = [row for row in agent_rows if todo_is_superseded(row, mapping)]
    superseded_ids = {str(row.get("todo_id")) for row in superseded}
    finished = [row for row in agent_rows
                if row.get("status") == "done" and str(row.get("todo_id")) not in superseded_ids]
    work = [row for row in finished if not _is_orchestrator_work(goal, row)]
    if not work:
        return {**base, "reason": GOAL_COMPLETE_NO_WORK}
    unaccepted = [str(row.get("todo_id")) for row in work
                  if delivery_requires_review(goal, row) and not accepted_by(row)]
    if unaccepted:
        return {**base, "reason": GOAL_COMPLETE_WAIT_ACCEPTANCE, "unaccepted_todo_ids": unaccepted[:20]}
    push_state = read_push_state(runtime_root, goal_id)
    try:
        declared = goal_repos(goal)
    except ValueError:
        declared = []
    repos = [_repo_completion(goal, repo, goal_id, push_state) for repo in declared]
    waiting = [repo["name"] for repo in repos if repo.get("push") == "pending"]
    if waiting:
        return {**base, "reason": GOAL_COMPLETE_WAIT_PUSH, "pending_repos": waiting}
    follow_ups = [
        {"todo_id": row.get("todo_id"), "task_class": row.get("task_class"),
         "text": str(row.get("text") or "")[:160]}
        for row in user_rows if row.get("status") == "open"
    ][:_FOLLOW_UP_LIMIT]
    todos = {
        "accepted": sum(1 for row in work if accepted_by(row)),
        "done_without_review": sum(1 for row in work if not delivery_requires_review(goal, row)),
        "rejects": sum(_int(row.get("reject_count")) for row in agent_rows),
        "superseded": len(superseded),
        "orchestrator_todos": len(finished) - len(work),
    }
    done_ids = [str(row.get("todo_id")) for row in agent_rows if row.get("status") == "done"]
    return {
        **base, "complete": True, "reason": "complete",
        "completion_key": _completion_key(done_ids, repos),
        "repos": repos, "todos": todos,
        "usage": _usage(Path(registry_path), Path(runtime_root), goal_id),
        "follow_ups": follow_ups,
    }


# --- opening ---------------------------------------------------------------------


_UNAVAILABLE = "unavailable"


def _number(value: Any) -> float | None:
    """A recorded figure, or None when it is missing or not a number (shown as unavailable, never as 0)."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return float(value)
    except OverflowError:
        return None


def _figure(value: Any, spec: str) -> str:
    number = _number(value)
    return _UNAVAILABLE if number is None else format(number, spec)


def _money(value: Any) -> str:
    number = _number(value)
    return _UNAVAILABLE if number is None else f"${number:,.2f}"


def _rows(value: Any) -> list[Mapping[str, Any]]:
    """The mapping entries of a list read from the gate index; anything else is skipped."""

    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _repo_state(repo: Mapping[str, Any]) -> str:
    merges = len(_rows(repo.get("merged_todo_commits")))
    push = {
        "pushed": f"pushed to {repo.get('remote')}",
        "up_to_date": f"already on {repo.get('remote')}",
        "declined": f"not pushed ({repo.get('push_decision') or 'reject'} at the push gate)",
        "local_only": "local only",
        "no_merges": "no merges",
        "nothing_to_push": "nothing of this goal to push",
    }.get(str(repo.get("push")), str(repo.get("push")))
    return f"{merges} merge(s) on {repo.get('branch') or '-'}, {push}"


def _repo_line(repo: Mapping[str, Any]) -> str:
    return f"{repo.get('name')}: {_repo_state(repo)}"


def _todos_text(todos: Mapping[str, Any]) -> str:
    return (f"{_figure(todos.get('accepted'), '.0f')} accepted, {_figure(todos.get('rejects'), '.0f')} reject(s), "
            f"{_figure(todos.get('superseded'), '.0f')} superseded")


def _usage_text(usage: Any) -> str:
    if not isinstance(usage, Mapping) or not usage:
        return _UNAVAILABLE
    per_todo = usage.get("cost_per_accepted_todo_usd")
    return (
        f"{_money(usage.get('cost_usd'))} ({_money(usage.get('cost_estimated_usd'))} estimated), "
        f"{_figure(usage.get('turns'), '.0f')} Turn(s), {_figure(usage.get('agent_hours'), '.2f')} agent-h"
        + (f", {_money(per_todo)}/accepted todo" if _number(per_todo) else "")
    )


def _gate_text(goal_id: str, snapshot: Mapping[str, Any]) -> str:
    """The gate label; the todo list shows its first 500 characters, so the repos come last."""

    repos = "; ".join(_repo_line(repo) for repo in snapshot["repos"]) or "none declared"
    follow_ups = snapshot.get("follow_ups") or []
    return (
        f"{GOAL_COMPLETE_GATE_TEXT_PREFIX}{goal_id} is accepted, merged and its push resolved. "
        f"Todos: {_todos_text(snapshot['todos'])}. Usage: {_usage_text(snapshot['usage'])}."
        + (f" {len(follow_ups)} open follow-up(s)." if follow_ups else "")
        + " Close the goal, add work (the note is the follow-up) or leave it open: `loopx gate resolve "
        f"--goal-id {goal_id} --todo-id <this gate> --option close_goal|add_work|leave_open [--note ...]`. "
        f"Repos: {repos}."
    )


def _open_user_todos(registry_path: Path, goal_id: str, runtime_root: Path) -> dict[str, str]:
    from .todos import list_goal_todos

    listed = list_goal_todos(registry_path=registry_path, goal_id=goal_id, role="user", status="open",
                             runtime_root_arg=str(runtime_root))
    return {str(item.get("todo_id")): str(item.get("text") or "")
            for item in listed.get("todos") or [] if isinstance(item, Mapping)}


def _gate_extra(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "completion_key": snapshot["completion_key"],
        "completion_repos": snapshot["repos"],
        "completion_todos": snapshot["todos"],
        "completion_usage": snapshot["usage"],
        "follow_ups": snapshot["follow_ups"],
        "options": list(GOAL_COMPLETE_GATE_OPTIONS),
    }


def open_goal_complete_gate(
    *, registry_path: Path, runtime_root: Path, goal_id: str, goal: Mapping[str, Any] | None = None,
    runtime_root_arg: str | None = None,
) -> dict[str, Any]:
    """Open the goal's ``goal_complete`` gate once per completion.

    Idempotent: an open goal_complete gate, or a gate already opened for the
    current completion (whatever the owner decided), is returned instead of a
    second one. An open gate that lost its index entry is adopted by its text.
    """

    from .agent_registry import orchestrator_agent_for_goal
    from .todos import add_goal_todo

    lock = _lock_path(runtime_root, goal_id)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(lock):
        entries = goal_complete_gate_entries(runtime_root, goal_id)
        open_todos = _open_user_todos(Path(registry_path), goal_id, runtime_root)
        for gate_id in entries:
            if gate_id in open_todos and not entries[gate_id].get("closed"):
                return {"opened": False, "reason": "goal_complete_gate_open", "gate_todo_id": gate_id}
        orphan = next((gate_id for gate_id, text in open_todos.items()
                       if text.startswith(GOAL_COMPLETE_GATE_TEXT_PREFIX) and gate_id not in entries), None)
        if orphan is not None:
            register_gate_kind(runtime_root, goal_id, orphan, kind=GATE_KIND_GOAL_COMPLETE,
                               extra={"options": list(GOAL_COMPLETE_GATE_OPTIONS), "adopted": True})
            return {"opened": False, "reason": "goal_complete_gate_adopted", "gate_todo_id": orphan}
        snapshot = goal_completion_snapshot(registry_path=registry_path, runtime_root=runtime_root,
                                            goal_id=goal_id, goal=goal, runtime_root_arg=runtime_root_arg)
        if not snapshot["complete"]:
            return {"opened": False, **{key: value for key, value in snapshot.items() if key != "complete"}}
        key = snapshot["completion_key"]
        for gate_id, entry in entries.items():
            if entry.get("completion_key") == key:
                return {"opened": False, "reason": "completion_already_gated", "gate_todo_id": gate_id,
                        "completion_key": key}
        text = _gate_text(goal_id, snapshot)
        goal_record = goal
        if goal_record is None:
            from .agent_registry import load_goal_from_registry

            goal_record = load_goal_from_registry(Path(registry_path), goal_id) or {}
        orchestrator = orchestrator_agent_for_goal(dict(goal_record))
        # The owner path (no agent id): LoopX opens this gate, like the push
        # and budget gates. It holds the orchestrator, whose lane is idle anyway.
        gate = add_goal_todo(
            registry_path=Path(registry_path), goal_id=goal_id, role="user", text=text,
            task_class="user_gate", blocks_agent=orchestrator, goal_bound=orchestrator is None,
            note="Opened by LoopX: the goal's work is merged and its push resolved (decision 42).",
            runtime_root_arg=runtime_root_arg or str(runtime_root),
        )
        gate_id = str(gate.get("todo_id") or "")
        register_gate_kind(runtime_root, goal_id, gate_id, kind=GATE_KIND_GOAL_COMPLETE,
                           extra=_gate_extra(snapshot))
    _event(runtime_root, goal_id, "goal_complete_opened", todo_id=gate_id, status="gate_opened", details={
        "gate_id": gate_id, "completion_key": key, "accepted_todos": snapshot["todos"]["accepted"],
        "repos": ",".join(str(repo.get("name")) for repo in snapshot["repos"]),
    })
    return {"opened": True, "gate_todo_id": gate_id, "gate_text": text, "completion_key": key}


# --- resolving -------------------------------------------------------------------


def resolve_goal_complete_option(decision: str | None, option: str | None) -> str:
    """The option a decision selects; an explicit option must fit its decision."""

    if option is None:
        if decision not in _GOAL_COMPLETE_DEFAULT_OPTION:
            raise ValueError(f"unsupported gate decision {decision!r} for a goal_complete gate")
        return _GOAL_COMPLETE_DEFAULT_OPTION[decision]
    if option not in GOAL_COMPLETE_OPTION_DECISIONS:
        raise ValueError(f"goal_complete gate option must be one of: {', '.join(GOAL_COMPLETE_GATE_OPTIONS)}")
    if decision is not None and decision != GOAL_COMPLETE_OPTION_DECISIONS[option]:
        raise ValueError(
            f"gate option {option} records decision {GOAL_COMPLETE_OPTION_DECISIONS[option]}, not {decision}"
        )
    return option


def goal_complete_gate_preflight(
    *, runtime_root: Path, goal_id: str, gate_todo_id: str, decision: str | None, option: str | None,
    note: str | None = None, registry_path: Path | None = None, runtime_root_arg: str | None = None,
) -> str | None:
    """Validate a decision on a ``goal_complete`` gate before it closes.

    Returns the selected option, or None for other gates. ``add_work`` needs
    a note: it is the follow-up the orchestrator plans. ``close_goal`` is
    refused when the goal changed after the gate opened (new work, a new
    merge or another open gate): the gate would close a goal it no longer
    describes. Leave it open instead; a new gate opens once that work is done.
    """

    entry = goal_complete_gate_entry(runtime_root, goal_id, gate_todo_id)
    if entry is None:
        return None
    selected = resolve_goal_complete_option(decision, option)
    if selected == GOAL_COMPLETE_OPTION_ADD_WORK and not str(note or "").strip():
        raise ValueError("add_work needs a note: the follow-up work the orchestrator should plan")
    if selected == GOAL_COMPLETE_OPTION_CLOSE and registry_path is not None:
        current = goal_completion_snapshot(
            registry_path=Path(registry_path), runtime_root=runtime_root, goal_id=goal_id,
            runtime_root_arg=runtime_root_arg, ignore_gate_ids=frozenset({str(gate_todo_id)}),
        )
        recorded = entry.get("completion_key")
        if not current["complete"] or (recorded and current.get("completion_key") != recorded):
            raise ValueError(
                f"the goal changed after this gate opened ({current.get('reason')}); close it with "
                "--option leave_open, and a new goal_complete gate opens once that work is done"
            )
    return selected


def _close_goal(registry_path: Path, goal_id: str, gate_todo_id: str, runtime_root_arg: str | None) -> dict[str, Any]:
    from .control_plane.goals.activation_service import set_goal_activation_state

    payload = set_goal_activation_state(
        registry_path=Path(registry_path), goal_id=goal_id, state="stopped",
        reason=f"goal complete; the owner closed it at gate {gate_todo_id}",
        runtime_root_override=runtime_root_arg, actor_kind="owner", execute=True,
    )
    return {key: payload.get(key) for key in ("ok", "changed", "written") if key in payload}


def _follow_up_marker(gate_todo_id: str) -> str:
    return f"(goal_complete gate {gate_todo_id})"


def _add_follow_up(registry_path: Path, goal_id: str, gate_todo_id: str, note: str,
                   runtime_root_arg: str | None) -> str | None:
    """One orchestrator action todo carrying the owner's follow-up (idempotent per gate)."""

    from .agent_registry import load_goal_from_registry, orchestrator_agent_for_goal
    from .dispatch.orchestrator_actions import ORCHESTRATOR_ACTION_TEXT_PREFIX
    from .todos import add_goal_todo, list_goal_todos

    marker = _follow_up_marker(gate_todo_id)
    listed = list_goal_todos(registry_path=Path(registry_path), goal_id=goal_id, role="agent",
                             runtime_root_arg=runtime_root_arg)
    for row in listed.get("todos") or []:
        if isinstance(row, Mapping) and marker in str(row.get("text") or ""):
            return str(row.get("todo_id") or "") or None
    orchestrator = orchestrator_agent_for_goal(dict(load_goal_from_registry(Path(registry_path), goal_id) or {}))
    if not orchestrator:
        return None
    clipped = " ".join(str(note).split())[:_FOLLOW_UP_NOTE_CHARS]
    text = (
        f"{ORCHESTRATOR_ACTION_TEXT_PREFIX}{GOAL_COMPLETE_FOLLOW_UP_LABEL}{clipped} {marker}. The owner added "
        "work after the goal's work was complete: plan it (propose a plan card or open todos) or ask back "
        "through a user gate."
    )
    added = add_goal_todo(
        registry_path=Path(registry_path), goal_id=goal_id, role="agent", runtime_root_arg=runtime_root_arg,
        text=text, task_class="advancement_task", action_kind="replan", claimed_by=str(orchestrator),
        role_contract={"required_role": "orchestrator", "requires_acceptance": False},
        note=f"User follow-up: {clipped}"[:600],
    )
    return str(added.get("todo_id") or "") or None


def settle_goal_complete_gate(
    *, registry_path: Path, runtime_root: Path, goal_id: str, gate_todo_id: str, decision: str | None,
    option: str | None, note: str | None = None, runtime_root_arg: str | None = None,
) -> dict[str, Any] | None:
    """After a ``goal_complete`` gate closed, apply the chosen option.

    Returns None for other gates. One settlement per gate runs at a time: a
    recorded outcome replays, and only a failed one is retried (with its
    recorded option); see ``gate_threads.run_gate_settlement``.
    """

    entry = goal_complete_gate_entry(runtime_root, goal_id, gate_todo_id)
    if entry is None:
        return None

    def pin(_decided: str | None, _selected: str) -> dict[str, Any]:
        return {"note": note or ""}  # pinned even when empty, so a retry never takes a new note

    def apply(decided: str | None, selected: str, prior: Mapping[str, Any]) -> dict[str, Any]:
        pinned_note = (prior.get("pinned") or {}).get("note", note)  # a retry keeps the first note
        outcome: dict[str, Any] = {"ok": True, "gate_todo_id": gate_todo_id, "decision": decided,
                                   "option": selected, "at": _now()}
        try:
            if selected == GOAL_COMPLETE_OPTION_CLOSE:
                outcome["goal_stop"] = _close_goal(Path(registry_path), goal_id, gate_todo_id, runtime_root_arg)
                if outcome["goal_stop"].get("ok") is False:
                    outcome.update(ok=False, error="the goal could not be closed; stop it with loopx goal-lifecycle")
                outcome["resume"] = (
                    f"loopx goal-lifecycle --goal-id {goal_id} --operation resume --actor-kind owner --execute"
                )
            elif selected == GOAL_COMPLETE_OPTION_ADD_WORK:
                follow_up = _add_follow_up(Path(registry_path), goal_id, gate_todo_id, str(pinned_note or ""),
                                           runtime_root_arg)
                outcome["follow_up_todo_id"] = follow_up
                if not follow_up:
                    outcome.update(ok=False, error="no orchestrator is registered to take the follow-up")
        except (OSError, ValueError) as error:  # the gate is closed; report, never raise
            outcome.update(ok=False, error=str(error)[:400])
        return outcome

    outcome, applied = run_gate_settlement(
        runtime_root, goal_id, gate_todo_id, decision=decision, option=resolve_goal_complete_option(decision, option),
        outcome_key="completion_outcome", apply=apply, pin=pin,
    )
    if not applied:
        return {"payload_key": "goal_complete", **outcome, "replayed": True}
    _event(runtime_root, goal_id, "goal_complete_decided", todo_id=gate_todo_id, status=outcome["option"], details={
        "gate_id": gate_todo_id, "option": outcome["option"], "ok": outcome["ok"],
        **({"follow_up_todo_id": outcome["follow_up_todo_id"]} if outcome.get("follow_up_todo_id") else {}),
    })
    return {"payload_key": "goal_complete", **outcome}


def goal_complete_hold(runtime_root: Path, goal_id: str, goal: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Why the dispatcher no longer considers this goal: the owner closed it, else None.

    Only while the goal is stopped and its latest decided goal_complete gate
    chose ``close_goal``; ``loopx goal-lifecycle --operation resume`` lifts it.
    """

    from .control_plane.goals.activation import goal_is_stopped

    if not goal_is_stopped(goal):
        return None
    decided = [
        (str((entry.get("completion_outcome") or {}).get("at") or ""), gate_id)
        for gate_id, entry in goal_complete_gate_entries(runtime_root, goal_id).items()
        if isinstance(entry.get("completion_outcome"), Mapping)
    ]
    if not decided:
        return None
    _, latest = max(decided)
    entry = goal_complete_gate_entry(runtime_root, goal_id, latest) or {}
    if (entry.get("completion_outcome") or {}).get("option") == GOAL_COMPLETE_OPTION_CLOSE:
        return {"reason": GOAL_COMPLETE_HOLD_CLOSED, "gate_todo_id": latest}
    return None


# --- Markdown (``gate show`` and ``gate resolve``) ------------------------------------------------


def completion_outcome_markdown(outcome: Mapping[str, Any]) -> list[str]:
    """What the owner's option did (the ``goal_complete`` payload or the gate's ``completion_outcome``)."""

    from .push_requests import redact_paths

    option = outcome.get("option")
    if not outcome.get("ok"):
        return [f"- outcome: {option} failed: {redact_paths(str(outcome.get('error')))}"]
    if option == GOAL_COMPLETE_OPTION_CLOSE:
        return [f"- outcome: {option}, goal stopped", f"- resume: `{outcome.get('resume')}`"]
    if option == GOAL_COMPLETE_OPTION_ADD_WORK:
        return [f"- outcome: {option}, follow-up todo `{outcome.get('follow_up_todo_id')}` for the orchestrator"]
    return [f"- outcome: {option}, the goal stays open"]


def render_goal_complete_markdown(view: Mapping[str, Any]) -> list[str]:
    """The ``gate show`` section of a goal_complete gate: each part of the summary it has, and its result.

    A gate adopted by its text has no snapshot, so only its options (and
    outcome) show; missing or malformed usage reads "unavailable", never $0.00.
    """

    lines = ["", "## Completion", ""]
    todos = view.get("completion_todos")
    if isinstance(todos, Mapping) and todos:
        lines.append(f"- todos: {_todos_text(todos)}" + "".join(
            f", {_figure(todos.get(key), '.0f')} {label}"
            for key, label in (("done_without_review", "done without review"),
                               ("orchestrator_todos", "orchestrator todo(s)"))
            if _number(todos.get(key))
        ))
    for repo in _rows(view.get("completion_repos")):
        lines.append(f"- `{repo.get('name')}`: {_repo_state(repo)}")
        commits = _rows(repo.get("merged_todo_commits"))
        if commits:
            lines.append("  - merged: " + ", ".join(
                f"{commit.get('sha')} {commit.get('todo_id') or ''}".strip() for commit in commits))
    usage = view.get("completion_usage")
    lines.append(f"- usage: {_usage_text(usage)}")
    if isinstance(usage, Mapping):
        lines += [f"  - {row.get('role')}: {_money(row.get('cost_usd'))}, {_figure(row.get('turns'), '.0f')} Turn(s)"
                  for row in _rows(usage.get("by_role"))]
    lines += [f"- open follow-up `{item.get('todo_id')}`: {item.get('text')}" for item in _rows(view.get("follow_ups"))]
    options = view.get("options")
    if isinstance(options, list) and options:
        lines.append("- options: " + ", ".join(str(option) for option in options))
    if isinstance(view.get("completion_outcome"), Mapping):
        lines += completion_outcome_markdown(view["completion_outcome"])
    return lines
