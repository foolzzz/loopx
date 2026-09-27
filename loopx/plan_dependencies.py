"""Plan dependency release and todo supersession (fork gap G6, decision 37).

Plan cards give each dependent todo a ``depends_on`` list; the todo is created
``deferred`` and :func:`loopx.plan_cards.resume_ready_plan_todos` reopens it
once every dependency is satisfied. Under role_v1 a dependency is satisfied
only by finished, reviewed work:

- a dependency that requires acceptance (an explicit ``requires_acceptance``,
  or the developer default when the goal has an acceptor) must be ``done``
  with the accept verdict's ``accepted_by=<acceptor>`` evidence. Accept merges
  the todo branch before it completes the todo, so that record is also the
  merge record. ``done`` through a manual or owner path does not count;
- any other dependency is satisfied by ``done``;
- a superseded dependency never counts as done.

Replacing or splitting a todo uses :func:`supersede_goal_todo_by`
(``loopx todo supersede --by NEW[,NEW2]``). It closes the old todo through the
kernel's own supersede transition (completion note ``superseded``) and records
a supersession: every todo that depended on the old one now depends on all of
its replacements. Supersessions are an append-only per-goal log folded into a
substitution map on every read, so the effective dependency graph is a pure
replay of the applied plans plus that log. Each supersession also appends a
``todo_dependency_rewrite`` rollout event.

peer_v1 goals are unaffected: ``done`` releases dependents, as before, and
``--by`` is refused.

Durable layout: ``<runtime_root>/goals/<goal>/plans/supersessions.jsonl``.
Every dependency resume pass also refreshes ``dependency-waits.json`` next to
it, a read model of why deferred plan todos still wait, for the role board.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .file_lock import exclusive_file_lock
from .history import validate_goal_id_path_segment
from .todo_review_blocked import OWNER_ACTOR

SUPERSESSION_SCHEMA = "loopx_todo_supersession_v0"
SUPERSESSION_LOG_NAME = "supersessions.jsonl"
DEPENDENCY_WAIT_SNAPSHOT_NAME = "dependency-waits.json"
DEPENDENCY_WAIT_SNAPSHOT_SCHEMA = "loopx_plan_dependency_waits_v0"
SUPERSEDED_COMPLETION_NOTE = "superseded"
DEPENDENCY_REWRITE_EVENT_KIND = "todo_dependency_rewrite"
MAX_SUPERSEDING_TODOS = 8

# ``accepted_by=<actor>`` then ``: <note>``, ``; <evidence>`` or the end; agent
# ids may contain ``:`` but never a space.
_ACCEPTED_BY_MARKER = re.compile(r"^accepted_by=([A-Za-z0-9][A-Za-z0-9_.:@-]*?)(?=: |;|$)")
_TODO_DONE_CONDITION = "todo_done:"


class SupersessionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def supersessions_path(runtime_root: Path, goal_id: str) -> Path:
    return (
        Path(runtime_root).expanduser() / "goals" / validate_goal_id_path_segment(goal_id)
        / "plans" / SUPERSESSION_LOG_NAME
    )


def read_supersessions(runtime_root: Path, goal_id: str) -> list[dict[str, Any]]:
    path = supersessions_path(runtime_root, goal_id)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn final line from an interrupted append
        if isinstance(record, dict) and record.get("schema_version") == SUPERSESSION_SCHEMA:
            records.append(record)
    return records


def supersession_map(records: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Fold the log: superseded todo id -> its replacement ids (first record wins)."""

    mapping: dict[str, list[str]] = {}
    for record in records:
        old = str(record.get("superseded_todo_id") or "")
        by = [str(item) for item in record.get("by_todo_ids") or [] if item]
        if old and by and old not in mapping:
            mapping[old] = by
    return mapping


def effective_dependencies(dependencies: Sequence[str], mapping: Mapping[str, Sequence[str]]) -> list[str]:
    """Replace every superseded dependency by its replacements, transitively."""

    result: list[str] = []

    def expand(todo_id: str, seen: frozenset[str]) -> None:
        if todo_id in seen:
            return
        if todo_id in mapping:
            for replacement in mapping[todo_id]:
                expand(replacement, seen | {todo_id})
        elif todo_id not in result:
            result.append(todo_id)

    for dependency in dependencies:
        if dependency:
            expand(str(dependency), frozenset())
    return result


def _plan_dependency_edges(runtime_root: Path, goal_id: str) -> list[dict[str, Any]]:
    """One entry per applied plan todo that declares dependencies."""

    from .plan_cards import list_plans

    edges: list[dict[str, Any]] = []
    for plan in list_plans(runtime_root, goal_id):
        if plan.get("status") != "applied":
            continue
        ids = plan.get("todo_id_map") if isinstance(plan.get("todo_id_map"), Mapping) else {}
        body = plan.get("plan") if isinstance(plan.get("plan"), Mapping) else {}
        for item in body.get("todos") or []:
            if not isinstance(item, Mapping) or not item.get("depends_on"):
                continue
            todo_id = str(ids.get(item.get("key")) or "")
            if todo_id:
                edges.append({
                    "todo_id": todo_id,
                    "plan_id": str(plan.get("plan_id") or ""),
                    "proposed_by": plan.get("proposed_by"),
                    "declared": [str(ids.get(key) or "") for key in item.get("depends_on") or []],
                })
    return edges


def accepted_by(row: Mapping[str, Any]) -> str | None:
    """The acceptor named by the accept verdict's completion evidence, if any."""

    match = _ACCEPTED_BY_MARKER.match(str(row.get("evidence") or ""))
    return match.group(1) if match else None


def _is_superseded(row: Mapping[str, Any], mapping: Mapping[str, Sequence[str]]) -> bool:
    return (
        str(row.get("todo_id") or "") in mapping
        or bool(row.get("superseded_by"))
        or str(row.get("note") or "").strip() == SUPERSEDED_COMPLETION_NOTE
    )


def dependency_wait_reason(
    goal: Mapping[str, Any] | None, dependency_id: str, row: Mapping[str, Any] | None,
    mapping: Mapping[str, Sequence[str]],
) -> str | None:
    """Why ``dependency_id`` does not release its dependents yet (``None``: satisfied)."""

    from .todo_acceptance import delivery_requires_review, goal_uses_role_v1, resolve_todo_acceptor

    if not dependency_id:
        return "a dependency has no todo id; the plan was not fully applied"
    if row is None:
        return f"dependency {dependency_id} is not in the active todo list"
    status = str(row.get("status") or "")
    if status != "done":
        return f"waiting for dependency {dependency_id} (status {status or 'unknown'})"
    if not goal_uses_role_v1(goal):
        return None
    if _is_superseded(row, mapping):
        return (
            f"dependency {dependency_id} was superseded without a replacement; the orchestrator "
            f"rewires its dependents with `loopx todo supersede --todo-id {dependency_id} --by NEW`"
        )
    if not delivery_requires_review(goal, row):
        return None
    acceptor = accepted_by(row)
    reviewer = resolve_todo_acceptor(goal, row)
    # The acceptor's verdict, or the owner's manual accept through an
    # acceptor-blocked gate (G12); both merge before they complete the todo.
    if acceptor and (
        acceptor in (reviewer["agent_id"], OWNER_ACTOR) or acceptor in reviewer["goal_acceptors"]
    ):
        return None
    return (
        f"dependency {dependency_id} is done without an accept+merge record; it requires "
        "acceptance, so its dependents wait until an acceptor accepts it (LoopX merges on "
        "accept) or the orchestrator supersedes it with `loopx todo supersede --by NEW`"
    )


def _goal(registry_path: Path, goal_id: str) -> dict[str, Any] | None:
    from .agent_registry import load_goal_from_registry

    return load_goal_from_registry(Path(registry_path), goal_id)


def _rows(registry_path: Path, goal_id: str, runtime_root_arg: str | None) -> dict[str, dict[str, Any]]:
    from .todos import list_goal_todos

    listed = list_goal_todos(registry_path=registry_path, goal_id=goal_id, runtime_root_arg=runtime_root_arg)
    return {str(row.get("todo_id")): row for row in listed.get("todos") or [] if isinstance(row, dict)}


def plan_dependency_states(
    *, registry_path: Path, goal_id: str, runtime_root: Path, runtime_root_arg: str | None = None,
    rows: Mapping[str, Mapping[str, Any]] | None = None, goal: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Every applied plan todo with dependencies: its effective deps and wait reasons."""

    edges = _plan_dependency_edges(runtime_root, goal_id)
    if not edges:
        return []
    goal = goal if goal is not None else _goal(registry_path, goal_id)
    rows = rows if rows is not None else _rows(registry_path, goal_id, runtime_root_arg)
    mapping = supersession_map(read_supersessions(runtime_root, goal_id))
    states = []
    for edge in edges:
        depends_on = effective_dependencies(edge["declared"], mapping)
        if any(not dependency for dependency in edge["declared"]):
            depends_on.append("")
        waiting = [
            reason for reason in (
                dependency_wait_reason(goal, dependency, rows.get(dependency), mapping)
                for dependency in depends_on
            ) if reason
        ]
        states.append({**edge, "depends_on": depends_on, "waiting": waiting})
    return states


def dependency_waits(
    *, registry_path: Path, goal_id: str, runtime_root: Path, runtime_root_arg: str | None = None,
    rows: Mapping[str, Mapping[str, Any]] | None = None, goal: Mapping[str, Any] | None = None,
) -> dict[str, list[str]]:
    """``todo_id -> reasons`` for deferred plan todos that still wait (role_v1 only)."""

    from .todo_acceptance import goal_uses_role_v1

    goal = goal if goal is not None else _goal(registry_path, goal_id)
    if not goal_uses_role_v1(goal):
        return {}
    rows = rows if rows is not None else _rows(registry_path, goal_id, runtime_root_arg)
    return {
        state["todo_id"]: state["waiting"]
        for state in plan_dependency_states(
            registry_path=registry_path, goal_id=goal_id, runtime_root=runtime_root,
            runtime_root_arg=runtime_root_arg, rows=rows, goal=goal,
        )
        if state["waiting"] and (rows.get(state["todo_id"]) or {}).get("status") == "deferred"
    }


def _snapshot_path(runtime_root: Path, goal_id: str) -> Path:
    return supersessions_path(runtime_root, goal_id).with_name(DEPENDENCY_WAIT_SNAPSHOT_NAME)


def write_dependency_wait_snapshot(runtime_root: Path, goal_id: str, waits: Mapping[str, Sequence[str]]) -> None:
    """Refresh the role board's wait read model (written only when it changes)."""

    from .registry import atomic_write_json

    path = _snapshot_path(runtime_root, goal_id)
    body = {str(todo_id): list(reasons) for todo_id, reasons in sorted(waits.items())}
    if read_dependency_wait_snapshot(runtime_root, goal_id) == body:
        return
    if not body and not path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"schema_version": DEPENDENCY_WAIT_SNAPSHOT_SCHEMA, "updated_at": _now(), "waits": body})


def read_dependency_wait_snapshot(runtime_root: Path, goal_id: str) -> dict[str, list[str]]:
    try:
        payload = json.loads(_snapshot_path(runtime_root, goal_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    waits = payload.get("waits") if isinstance(payload, dict) else None
    if not isinstance(waits, dict):
        return {}
    return {
        str(todo_id): [str(reason) for reason in reasons if isinstance(reason, str)]
        for todo_id, reasons in waits.items() if isinstance(reasons, list)
    }


def annotate_todo_list_dependency_waits(
    payload: dict[str, Any], *, registry_path: Path, goal_id: str, runtime_root_arg: str | None,
) -> dict[str, Any]:
    """Add ``dependency_wait`` to waiting rows of a ``todo list`` payload (role_v1)."""

    from .control_plane.coordination.local_authority_shadow_adapter import effective_runtime_root

    try:
        waits = dependency_waits(
            registry_path=registry_path, goal_id=goal_id,
            runtime_root=effective_runtime_root(registry_path, runtime_root_arg),
            runtime_root_arg=runtime_root_arg,
        )
    except (OSError, ValueError):
        return payload  # the dependency view is advisory; never break the list
    if not waits:
        return payload
    payload["dependency_waits"] = [{"todo_id": todo_id, "reasons": reasons} for todo_id, reasons in waits.items()]
    items = list(payload.get("todos") or [])
    for key in ("agent_todos", "user_todos"):
        summary = payload.get(key)
        if isinstance(summary, dict):
            items.extend(summary.get("items") or [])
    for item in items:
        if isinstance(item, dict) and str(item.get("todo_id")) in waits:
            item["dependency_wait"] = "; ".join(waits[str(item["todo_id"])])
    return payload


# --- supersession --------------------------------------------------------------


def _require_supersede_actor(goal: Mapping[str, Any] | None, agent_id: str | None) -> str | None:
    from .agent_registry import orchestrator_agent_for_goal
    from .todo_acceptance import goal_uses_role_v1

    if not goal_uses_role_v1(goal):
        raise SupersessionError(
            "role_v1_required",
            "todo supersede --by requires a role_v1 goal; use todo supersede with --next-agent-todo",
        )
    if agent_id is None:
        return None  # the owner or CLI
    orchestrator = orchestrator_agent_for_goal(dict(goal or {}))
    if agent_id != orchestrator:
        raise SupersessionError(
            "not_orchestrator",
            f"agent {agent_id!r} cannot supersede todos in goal {(goal or {}).get('id')!r}; only its "
            f"orchestrator ({orchestrator or 'none registered'}) or the owner (no --agent-id) can",
        )
    return agent_id


def _normalize_by(by: Sequence[str] | str) -> list[str]:
    from .control_plane.todos.contract import normalize_todo_id

    raw = by.split(",") if isinstance(by, str) else [part for item in by for part in str(item).split(",")]
    result: list[str] = []
    for item in raw:
        text = item.strip()
        if not text:
            continue
        todo_id = normalize_todo_id(text)
        if not todo_id:
            raise SupersessionError("invalid_todo_id", f"--by {text!r} is not a todo id")
        if todo_id not in result:
            result.append(todo_id)
    if not result:
        raise SupersessionError("by_required", "todo supersede --by needs at least one replacement todo id")
    if len(result) > MAX_SUPERSEDING_TODOS:
        raise SupersessionError("too_many_replacements", f"--by names at most {MAX_SUPERSEDING_TODOS} todos")
    return result


def _dependents(
    old: str, edges: Sequence[Mapping[str, Any]], rows: Mapping[str, Mapping[str, Any]],
    mapping: Mapping[str, Sequence[str]],
) -> list[str]:
    """Unfinished todos that depend on ``old``: plan edges plus ``resume_when=todo_done:old``."""

    found: list[str] = []
    for edge in edges:
        if old in effective_dependencies(edge["declared"], mapping) and edge["todo_id"] not in found:
            found.append(edge["todo_id"])
    for todo_id, row in rows.items():
        if row.get("resume_when") == _TODO_DONE_CONDITION + old and todo_id not in found:
            found.append(todo_id)
    return [todo_id for todo_id in found if (rows.get(todo_id) or {}).get("status") != "done"]


def _transitive_dependents(
    old: str, edges: Sequence[Mapping[str, Any]], mapping: Mapping[str, Sequence[str]],
) -> set[str]:
    graph = {edge["todo_id"]: set(effective_dependencies(edge["declared"], mapping)) for edge in edges}
    reached: set[str] = set()
    frontier = {old}
    while frontier:
        nxt = {todo_id for todo_id, deps in graph.items() if deps & frontier} - reached
        reached |= nxt
        frontier = nxt
    return reached


def _append_supersession(runtime_root: Path, goal_id: str, record: Mapping[str, Any]) -> bool:
    """Append ``record`` unless the old todo already has one. Returns whether it was written."""

    path = supersessions_path(runtime_root, goal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(path):
        if str(record["superseded_todo_id"]) in supersession_map(read_supersessions(runtime_root, goal_id)):
            return False
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record), sort_keys=True, ensure_ascii=False) + "\n")
    return True


def _rewrite_event(runtime_root: Path, goal_id: str, record: Mapping[str, Any]) -> None:
    from .rollout_event_log import append_rollout_event, build_rollout_event, rollout_event_log_path

    event = build_rollout_event(
        goal_id=goal_id, event_kind=DEPENDENCY_REWRITE_EVENT_KIND, agent_id=record.get("actor"),
        todo_id=str(record["superseded_todo_id"]), status=SUPERSEDED_COMPLETION_NOTE,
        summary=f"todo {record['superseded_todo_id']} superseded by {', '.join(record['by_todo_ids'])}",
        details={
            "superseded_todo_id": str(record["superseded_todo_id"]),
            "by_todo_ids": ",".join(record["by_todo_ids"]),
            "rewired_todo_ids": ",".join(record.get("rewired_todo_ids") or []),
        },
    )
    append_rollout_event(rollout_event_log_path(runtime_root, goal_id), event)


def supersede_goal_todo_by(
    *, registry_path: Path, goal_id: str, todo_id: str, by: Sequence[str] | str,
    agent_id: str | None = None, note: str | None = None, runtime_root_arg: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Replace (``by`` = one id) or split (several ids) a todo, rewiring its dependents.

    The old todo is closed with the kernel supersede transition, which is not
    counted as done for dependency release. Every todo that depended on it now
    depends on all of ``by``. Only the role_v1 orchestrator, or the owner with
    no agent id, may supersede. Retrying an applied supersession is a no-op.
    """

    from .control_plane.coordination.local_authority_shadow_adapter import effective_runtime_root
    from .control_plane.todos.contract import compact_todo_text, normalize_todo_id
    from .todos import supersede_goal_todo, update_goal_todo

    goal = _goal(registry_path, goal_id)
    if goal is None:
        raise SupersessionError("goal_not_registered", f"goal {goal_id!r} is not in the registry")
    actor = _require_supersede_actor(goal, agent_id)
    old = normalize_todo_id(todo_id) or ""
    if not old:
        raise SupersessionError("invalid_todo_id", f"--todo-id {todo_id!r} is not a todo id")
    replacements = _normalize_by(by)
    if old in replacements:
        raise SupersessionError("self_supersede", f"todo {old} cannot supersede itself")
    runtime_root = effective_runtime_root(registry_path, runtime_root_arg)
    rows = _rows(registry_path, goal_id, runtime_root_arg)
    mapping = supersession_map(read_supersessions(runtime_root, goal_id))
    base = {"ok": True, "goal_id": goal_id, "todo_id": old, "superseded_by": replacements, "dry_run": dry_run}
    if old in mapping:
        if mapping[old] != replacements:
            raise SupersessionError(
                "already_superseded", f"todo {old} is already superseded by {', '.join(mapping[old])}",
            )
        return {**base, "changed": False, "already_superseded": True, "rewired_todo_ids": []}
    old_row = rows.get(old)
    if old_row is None or old_row.get("role") != "agent":
        raise SupersessionError("todo_not_found", f"agent todo {old} was not found")
    for replacement in replacements:
        row = rows.get(replacement)
        if row is None or row.get("role") != "agent":
            raise SupersessionError("replacement_not_found", f"replacement agent todo {replacement} was not found")
        if row.get("status") == "done" or replacement in mapping:
            raise SupersessionError(
                "replacement_finished",
                f"replacement {replacement} is already finished or superseded; name unfinished work",
            )
    edges = _plan_dependency_edges(runtime_root, goal_id)
    cyclic = sorted(set(replacements) & _transitive_dependents(old, edges, mapping))
    if cyclic:
        raise SupersessionError(
            "dependency_cycle", f"{', '.join(cyclic)} depends on {old}; it cannot replace it",
        )
    dependents = _dependents(old, edges, rows, mapping)
    record = {
        "schema_version": SUPERSESSION_SCHEMA, "goal_id": goal_id, "superseded_todo_id": old,
        "by_todo_ids": replacements, "rewired_todo_ids": dependents, "actor": actor,
        "note": compact_todo_text(note) if note else None, "at": _now(),
    }
    if dry_run:
        return {**base, "changed": True, "rewired_todo_ids": dependents, "would_close": old_row.get("status") != "done"}
    closed = None
    if old_row.get("status") != "done":
        # Like the accept verdict, the lifecycle write is attributed to the
        # claim owner so the kernel claim fence is unchanged; the
        # orchestrator's authority was checked above and is in the record.
        closed = supersede_goal_todo(
            registry_path=registry_path, goal_id=goal_id, todo_id=old, role="agent",
            runtime_root_arg=runtime_root_arg,
            reason=compact_todo_text(note) if note else f"superseded by {', '.join(replacements)}",
            agent_id=old_row.get("claimed_by") or actor,
        )
        if closed.get("ok") is False:
            return {**base, **closed, "ok": False, "rewired_todo_ids": []}
    written = _append_supersession(runtime_root, goal_id, record)
    # Keep the kernel's single resume_when condition in step with the rewire.
    for dependent in dependents:
        row = rows.get(dependent) or {}
        if row.get("resume_when") == _TODO_DONE_CONDITION + old and row.get("status") == "deferred":
            edge = next((item for item in edges if item["todo_id"] == dependent), {})
            update_goal_todo(
                registry_path=registry_path, goal_id=goal_id, todo_id=dependent, role="agent",
                runtime_root_arg=runtime_root_arg, resume_when=_TODO_DONE_CONDITION + replacements[-1],
                agent_id=row.get("claimed_by") or edge.get("proposed_by") or actor,
            )
    if written:
        _rewrite_event(runtime_root, goal_id, record)
    return {
        **base, "changed": True, "rewired_todo_ids": dependents,
        "closed": bool(closed), "supersession": record,
    }
