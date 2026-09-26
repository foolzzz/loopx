"""Plan cards (fork slice S6, design decision 12).

The orchestrator proposes a plan (``loopx plan propose``). The plan is stored
as a pending record and a user gate of kind ``plan_approval`` is opened for
it. Discussion and change requests happen in the gate thread; the orchestrator
revises the plan in place (``--revise``). The owner closes the gate:

- **approve** applies the plan: its todos are created in plan order and each
  plan key is mapped to the created todo id. The plan is validated (dry-run)
  before the gate is allowed to close, so an invalid plan keeps the gate open.
- **reject** / **cancel** apply nothing.

Application is exactly-once. A Markdown (legacy) goal writes the whole batch
in one locked state-file write. A goal with promoted canonical authority
creates each todo with a deterministic operation id
(``plan-<plan_id>-<key>``), so an interrupted apply resumes on retry without
duplicates; the plan record stays ``applying`` until every todo exists.

Durable layout: ``<runtime_root>/goals/<goal>/plans/<plan_id>.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .file_lock import exclusive_file_lock
from .gate_threads import (
    AUTHOR_ORCHESTRATOR,
    GATE_KIND_PLAN_APPROVAL,
    GateThreadError,
    append_gate_message,
    read_gate_index,
    register_gate_kind,
    require_goal_orchestrator,
)
from .history import validate_goal_id_path_segment
from .registry import atomic_write_json

PLAN_SCHEMA = "loopx_plan_card_v0"
PLAN_INPUT_SCHEMA = "loopx_plan_card_input_v0"

PLAN_PENDING = "pending"
PLAN_APPLYING = "applying"
PLAN_APPLIED = "applied"
PLAN_REJECTED = "rejected"
PLAN_CANCELLED = "cancelled"

MAX_PLAN_TODOS = 50
_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PLAN_ID_PATTERN = re.compile(r"^plan_[0-9a-f]{12}$")
_TODO_FIELDS = {
    "key", "text", "priority", "required_role", "bound_agent", "acceptor_agent",
    "depends_on", "successors", "requires_acceptance", "task_repositories",
    "acceptance", "validation_command", "estimated_effort", "action_kind",
}
_PLAN_FIELDS = {"schema_version", "title", "summary", "todos"}
_ROLES = ("developer", "acceptor", "orchestrator")


class PlanCardError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def plans_dir(runtime_root: Path, goal_id: str) -> Path:
    return Path(runtime_root).expanduser() / "goals" / validate_goal_id_path_segment(goal_id) / "plans"


def plan_path(runtime_root: Path, goal_id: str, plan_id: str) -> Path:
    if not _PLAN_ID_PATTERN.fullmatch(str(plan_id or "")):
        raise PlanCardError("invalid_plan_id", f"invalid plan id {plan_id!r}")
    return plans_dir(runtime_root, goal_id) / f"{plan_id}.json"


def read_plan(runtime_root: Path, goal_id: str, plan_id: str) -> dict[str, Any]:
    path = plan_path(runtime_root, goal_id, plan_id)
    if not path.exists():
        raise PlanCardError("plan_not_found", f"plan {plan_id!r} was not found in goal {goal_id!r}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_plans(runtime_root: Path, goal_id: str) -> list[dict[str, Any]]:
    directory = plans_dir(runtime_root, goal_id)
    if not directory.exists():
        return []
    plans = []
    for path in sorted(directory.glob("plan_*.json")):
        try:
            plans.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(plans, key=lambda plan: str(plan.get("created_at") or ""))


# --- validation -------------------------------------------------------------


def _text(value: Any, field: str, *, limit: int, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        if required:
            raise PlanCardError("invalid_plan", f"{field} must be a non-empty string")
        return None
    text = " ".join(value.split())
    if len(text) > limit:
        raise PlanCardError("invalid_plan", f"{field} must be at most {limit} characters")
    return text


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise PlanCardError("invalid_plan", f"{field} must be a list of strings")
    return [item.strip() for item in value]


def normalize_plan(raw: Any, *, goal: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a proposed plan against the goal's agents, roles and repos."""

    from .agent_registry import agent_roles_for_goal, registered_agent_ids_for_goal
    from .workspace.repos import goal_repos

    if not isinstance(raw, dict):
        raise PlanCardError("invalid_plan", "plan must be a JSON object")
    unknown = sorted(set(raw) - _PLAN_FIELDS)
    if unknown:
        raise PlanCardError("invalid_plan", f"unknown plan field(s): {', '.join(unknown)}")
    if raw.get("schema_version") not in (None, PLAN_INPUT_SCHEMA):
        raise PlanCardError("invalid_plan", f"plan schema_version must be {PLAN_INPUT_SCHEMA}")
    title = _text(raw.get("title"), "title", limit=160)
    summary = _text(raw.get("summary"), "summary", limit=2000, required=False)
    items = raw.get("todos")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_PLAN_TODOS:
        raise PlanCardError("invalid_plan", f"plan todos must be a list of 1..{MAX_PLAN_TODOS} items")

    registered = set(registered_agent_ids_for_goal(dict(goal)))
    roles = agent_roles_for_goal(dict(goal))
    repos = {repo["name"] for repo in goal_repos(dict(goal))}
    todos: list[dict[str, Any]] = []
    keys: list[str] = []
    texts: set[str] = set()
    for index, item in enumerate(items):
        label = f"todos[{index}]"
        if not isinstance(item, dict):
            raise PlanCardError("invalid_plan", f"{label} must be an object")
        extra = sorted(set(item) - _TODO_FIELDS)
        if extra:
            raise PlanCardError("invalid_plan", f"{label} has unknown field(s): {', '.join(extra)}")
        key = str(item.get("key") or "").strip()
        if not _KEY_PATTERN.fullmatch(key):
            raise PlanCardError("invalid_plan", f"{label}.key must match {_KEY_PATTERN.pattern}")
        if key in keys:
            raise PlanCardError("invalid_plan", f"duplicate plan key {key!r}")
        text = _text(item.get("text"), f"{label}.text", limit=600)
        assert text is not None
        if text in texts:
            raise PlanCardError("invalid_plan", f"duplicate todo text in plan: {text!r}")
        role = str(item.get("required_role") or "developer").strip().lower()
        if role not in _ROLES:
            raise PlanCardError("invalid_plan", f"{label}.required_role must be one of: {', '.join(_ROLES)}")
        bound = item.get("bound_agent")
        if bound is not None:
            bound = str(bound).strip()
            if bound not in registered:
                raise PlanCardError("invalid_plan", f"{label}.bound_agent {bound!r} is not a registered agent")
            if roles.get(bound) and roles[bound] != role:
                raise PlanCardError(
                    "invalid_plan",
                    f"{label}.bound_agent {bound!r} has role {roles[bound]!r}, not required_role {role!r}",
                )
        acceptor = item.get("acceptor_agent")
        if acceptor is not None:
            acceptor = str(acceptor).strip()
            if acceptor not in registered:
                raise PlanCardError("invalid_plan", f"{label}.acceptor_agent {acceptor!r} is not a registered agent")
            if roles.get(acceptor) not in (None, "acceptor"):
                raise PlanCardError("invalid_plan", f"{label}.acceptor_agent {acceptor!r} is not an acceptor")
        requires_acceptance = item.get("requires_acceptance")
        if requires_acceptance is not None and not isinstance(requires_acceptance, bool):
            raise PlanCardError("invalid_plan", f"{label}.requires_acceptance must be a boolean")
        task_repositories = _string_list(item.get("task_repositories"), f"{label}.task_repositories")
        unknown_repos = [name for name in task_repositories if name not in repos]
        if unknown_repos:
            raise PlanCardError(
                "invalid_plan",
                f"{label}.task_repositories names unknown Goal repos: {', '.join(unknown_repos)}; "
                f"declared: {', '.join(sorted(repos)) or '(none)'}",
            )
        priority = item.get("priority")
        if priority is not None and str(priority) not in {"P0", "P1", "P2", "P3"}:
            raise PlanCardError("invalid_plan", f"{label}.priority must be P0..P3")
        todos.append({
            "key": key,
            "text": text,
            "priority": str(priority) if priority is not None else None,
            "required_role": role,
            "bound_agent": bound,
            "acceptor_agent": acceptor,
            "depends_on": _string_list(item.get("depends_on"), f"{label}.depends_on"),
            "successors": _string_list(item.get("successors"), f"{label}.successors"),
            "requires_acceptance": requires_acceptance,
            "task_repositories": task_repositories,
            "acceptance": _text(item.get("acceptance"), f"{label}.acceptance", limit=1000, required=False),
            "validation_command": _text(
                item.get("validation_command"), f"{label}.validation_command", limit=1000, required=False
            ),
            "estimated_effort": _text(item.get("estimated_effort"), f"{label}.estimated_effort", limit=80, required=False),
            "action_kind": _text(item.get("action_kind"), f"{label}.action_kind", limit=40, required=False),
        })
        keys.append(key)
        texts.add(text)

    # successors are the inverse of depends_on; fold them into one relation.
    position = {key: index for index, key in enumerate(keys)}
    depends: dict[str, list[str]] = {todo["key"]: list(todo["depends_on"]) for todo in todos}
    for todo in todos:
        for successor in todo["successors"]:
            if successor not in position:
                raise PlanCardError("invalid_plan", f"{todo['key']}.successors names unknown key {successor!r}")
            if todo["key"] not in depends[successor]:
                depends[successor].append(todo["key"])
    for todo in todos:
        for dependency in depends[todo["key"]]:
            if dependency not in position:
                raise PlanCardError("invalid_plan", f"{todo['key']}.depends_on names unknown key {dependency!r}")
            if position[dependency] >= position[todo["key"]]:
                raise PlanCardError(
                    "invalid_plan",
                    f"{todo['key']} depends on {dependency!r}, which is not listed earlier; "
                    "list plan todos in dependency order",
                )
        todo["depends_on"] = sorted(depends[todo["key"]], key=position.__getitem__)
        del todo["successors"]
    return {"title": title, "summary": summary, "todos": todos}


def _plan_digest(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# --- todo materialization ---------------------------------------------------


def _todo_note(plan_id: str, todo: Mapping[str, Any]) -> str:
    # Fork G2: the plan's acceptance goes to the todo's orchestrator-owned
    # acceptance_criteria field, not to the mutable note.
    parts = [f"Plan {plan_id} item {todo['key']}."]
    if todo.get("estimated_effort"):
        parts.append(f"Estimate: {todo['estimated_effort']}")
    if todo.get("depends_on"):
        parts.append("Depends on plan items: " + ", ".join(todo["depends_on"]))
    return " ".join(parts)


def _add_kwargs(plan_id: str, todo: Mapping[str, Any], ids: Mapping[str, str]) -> dict[str, Any]:
    role_contract: dict[str, Any] = {"required_role": todo["required_role"]}
    if todo.get("requires_acceptance") is not None:
        role_contract["requires_acceptance"] = todo["requires_acceptance"]
    if todo.get("acceptor_agent"):
        role_contract["acceptor_agent"] = todo["acceptor_agent"]
    if todo.get("task_repositories"):
        role_contract["task_repositories"] = list(todo["task_repositories"])
    if todo.get("acceptance"):
        role_contract["acceptance_criteria"] = todo["acceptance"]
    kwargs: dict[str, Any] = {
        "role": "agent",
        "text": todo["text"],
        "priority": todo.get("priority"),
        "claimed_by": todo.get("bound_agent"),
        "action_kind": todo.get("action_kind"),
        "validation_command": todo.get("validation_command"),
        "note": _todo_note(plan_id, todo),
        "role_contract": role_contract,
    }
    # Todos wait on their last dependency (plan order is topological, so the
    # last listed dependency finishes no earlier than the others start).
    # resume_when carries one condition; the full list is kept in the note.
    if todo.get("depends_on"):
        target = ids.get(todo["depends_on"][-1])
        kwargs["status"] = "deferred"
        kwargs["resume_when"] = f"todo_done:{target}" if target else None
    return kwargs


def _dry_run_validate(registry_path: Path, goal_id: str, plan_id: str, plan: Mapping[str, Any]) -> None:
    from .todos import add_goal_todo

    for todo in plan["todos"]:
        kwargs = _add_kwargs(plan_id, todo, {})
        kwargs.pop("status", None)
        kwargs.pop("resume_when", None)
        try:
            result = add_goal_todo(registry_path=registry_path, goal_id=goal_id, dry_run=True, **kwargs)
        except ValueError as error:
            raise PlanCardError("plan_item_invalid", f"plan item {todo['key']!r}: {error}") from None
        if not result.get("ok", True):
            raise PlanCardError("plan_item_invalid", f"plan item {todo['key']!r}: {result.get('error')}")


def _apply_legacy_batch(
    registry_path: Path, runtime_root: Path, goal_id: str, plan_id: str, plan: Mapping[str, Any],
) -> dict[str, str]:
    """One locked Markdown write for the whole plan (all or nothing)."""

    from .control_plane.coordination.legacy_writer_fence import legacy_todo_write_transaction
    from .control_plane.coordination.runtime_shadow_writer_adapter import (
        begin_todo_runtime_shadow_capture,
        settle_todo_runtime_shadow_capture,
        write_captured_todo_state,
    )
    from .control_plane.todos.active_state_editing import replace_updated_at
    from .control_plane.todos.next_action_runtime import apply_added_todo_next_action
    from .control_plane.todos.path_resolution import resolve_todo_state_path
    from .control_plane.todos.text import plan_todo_priority
    from .state_refresh import now_local
    from .todos import add_todo_to_lines

    _project, state = resolve_todo_state_path(registry_path=registry_path, goal_id=goal_id)
    ids: dict[str, str] = {}
    with legacy_todo_write_transaction(
        registry_path, goal_id, state, None, "todo_add", False, runtime_root=runtime_root,
    ):
        original = state.read_text(encoding="utf-8")
        capture = begin_todo_runtime_shadow_capture(
            registry_path=registry_path, runtime_root=runtime_root, goal_id=goal_id,
            state_path=state, write_class="todo_add", original_text=original,
        )
        lines = original.splitlines()
        updated_at = now_local()
        changed = False
        for todo in plan["todos"]:
            kwargs = _add_kwargs(plan_id, todo, ids)
            priority_plan = plan_todo_priority(
                {}, {"text": kwargs.pop("text"), **({"priority": kwargs["priority"]} if kwargs.get("priority") else {})}
            )
            kwargs.pop("priority", None)
            result = add_todo_to_lines(
                lines, text=str(priority_plan["text"]), updated_at=updated_at, **kwargs,
            )
            if not result.get("todo_id"):
                raise PlanCardError("plan_apply_failed", f"plan item {todo['key']!r} produced no todo id")
            ids[todo["key"]] = str(result["todo_id"])
            changed = apply_added_todo_next_action(lines, role="agent", add_result=result) or changed
        if changed:
            text = replace_updated_at("\n".join(lines) + ("\n" if original.endswith("\n") else ""), updated_at)
            write_captured_todo_state(
                capture, runtime_root=runtime_root, goal_id=goal_id, state_path=state, text=text,
            )
    settle_todo_runtime_shadow_capture(
        {"ok": True}, registry_path=registry_path, runtime_root=runtime_root,
        goal_id=goal_id, capture=capture,
    )
    return ids


def _apply_canonical_sequence(
    registry_path: Path, goal_id: str, plan_id: str, plan: Mapping[str, Any],
    runtime_root_arg: str | None,
) -> dict[str, str]:
    from .todos import add_goal_todo

    ids: dict[str, str] = {}
    for todo in plan["todos"]:
        result = add_goal_todo(
            registry_path=registry_path, goal_id=goal_id, runtime_root_arg=runtime_root_arg,
            operation_id=f"plan-{plan_id}-{todo['key']}", **_add_kwargs(plan_id, todo, ids),
        )
        if not result.get("todo_id"):
            raise PlanCardError("plan_apply_failed", f"plan item {todo['key']!r} produced no todo id")
        ids[todo["key"]] = str(result["todo_id"])
    return ids


# --- lifecycle --------------------------------------------------------------


def _goal(registry_path: Path, goal_id: str) -> dict[str, Any]:
    from .agent_registry import load_goal_from_registry

    goal = load_goal_from_registry(Path(registry_path), goal_id)
    if goal is None:
        raise PlanCardError("goal_not_registered", f"goal {goal_id!r} is not in the registry")
    return goal


def _event(runtime_root: Path, goal_id: str, kind: str, plan: Mapping[str, Any], **extra: Any) -> None:
    from .rollout_event_log import append_rollout_event, build_rollout_event, rollout_event_log_path

    event = build_rollout_event(
        goal_id=goal_id, event_kind=kind, agent_id=plan.get("proposed_by"),
        todo_id=plan.get("gate_todo_id"), gate_id=plan.get("gate_todo_id"),
        status=str(plan.get("status")),
        details={"plan_id": str(plan["plan_id"]), "revision": int(plan.get("revision") or 1), **extra},
    )
    append_rollout_event(rollout_event_log_path(runtime_root, goal_id), event)


def propose_plan(
    *, registry_path: Path, runtime_root: Path, goal_id: str, agent_id: str,
    plan: Any, revise_plan_id: str | None = None, runtime_root_arg: str | None = None,
) -> dict[str, Any]:
    """Store a pending plan and open (or reuse) its plan_approval gate."""

    from .todos import add_goal_todo

    goal = _goal(registry_path, goal_id)
    try:
        require_goal_orchestrator(goal, agent_id)
    except GateThreadError as error:
        raise PlanCardError(error.code, str(error)) from None
    normalized = normalize_plan(plan, goal=goal)
    digest = _plan_digest(normalized)

    if revise_plan_id:
        path = plan_path(runtime_root, goal_id, revise_plan_id)
        with exclusive_file_lock(path):
            record = read_plan(runtime_root, goal_id, revise_plan_id)
            if record.get("status") != PLAN_PENDING:
                raise PlanCardError(
                    "plan_not_pending", f"plan {revise_plan_id!r} is {record.get('status')}; only a pending plan can be revised"
                )
            if record.get("digest") == digest:
                return {"ok": True, "changed": False, "plan": record}
            _dry_run_validate(registry_path, goal_id, revise_plan_id, normalized)
            record["revisions"] = [
                *record.get("revisions", []),
                {"revision": record.get("revision", 1), "digest": record.get("digest"), "plan": record.get("plan")},
            ]
            record.update({
                "plan": normalized, "digest": digest, "revision": int(record.get("revision", 1)) + 1,
                "updated_at": _now(),
            })
            atomic_write_json(path, record)
        append_gate_message(
            runtime_root, goal_id, record["gate_todo_id"], author=AUTHOR_ORCHESTRATOR, agent_id=agent_id,
            text=f"Revised plan {record['plan_id']} to revision {record['revision']} "
            f"({len(normalized['todos'])} todos). See `loopx plan show --goal-id {goal_id} --plan-id {record['plan_id']}`.",
        )
        _event(runtime_root, goal_id, "plan_proposed", record)
        return {"ok": True, "changed": True, "plan": record}

    for existing in list_plans(runtime_root, goal_id):
        if existing.get("status") == PLAN_PENDING and existing.get("digest") == digest:
            return {"ok": True, "changed": False, "plan": existing}

    created_at = _now()
    plan_id = "plan_" + hashlib.sha256(f"{goal_id}|{created_at}|{digest}".encode()).hexdigest()[:12]
    _dry_run_validate(registry_path, goal_id, plan_id, normalized)
    gate = add_goal_todo(
        registry_path=registry_path, goal_id=goal_id, runtime_root_arg=runtime_root_arg,
        role="user", task_class="user_gate", agent_id=agent_id,
        text=f"Approve plan: {normalized['title']} ({len(normalized['todos'])} todos) [{plan_id}]",
        note=(normalized.get("summary") or f"Review with `loopx plan show --goal-id {goal_id} --plan-id {plan_id}`.")[:600],
    )
    if not gate.get("ok", True) or not gate.get("todo_id"):
        raise PlanCardError("plan_gate_failed", str(gate.get("error") or "could not open the plan approval gate"))
    record = {
        "schema_version": PLAN_SCHEMA,
        "plan_id": plan_id,
        "goal_id": goal_id,
        "status": PLAN_PENDING,
        "revision": 1,
        "digest": digest,
        "proposed_by": agent_id,
        "gate_todo_id": gate["todo_id"],
        "created_at": created_at,
        "updated_at": created_at,
        "plan": normalized,
        "todo_id_map": {},
    }
    path = plan_path(runtime_root, goal_id, plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, record)
    register_gate_kind(runtime_root, goal_id, gate["todo_id"], kind=GATE_KIND_PLAN_APPROVAL, plan_id=plan_id)
    append_gate_message(
        runtime_root, goal_id, gate["todo_id"], author=AUTHOR_ORCHESTRATOR, agent_id=agent_id,
        text=f"Proposed plan {plan_id}: {normalized['title']} ({len(normalized['todos'])} todos). "
        f"Approve, reject, cancel, or reply here to request changes.",
    )
    _event(runtime_root, goal_id, "plan_proposed", record)
    return {"ok": True, "changed": True, "plan": record}


def apply_plan(
    *, registry_path: Path, runtime_root: Path, goal_id: str, plan_id: str,
    runtime_root_arg: str | None = None,
) -> dict[str, Any]:
    """Apply an approved (or interrupted) plan exactly once."""

    from .control_plane.coordination.local_authority import read_canonical_todos_if_promoted

    path = plan_path(runtime_root, goal_id, plan_id)
    with exclusive_file_lock(path):
        record = read_plan(runtime_root, goal_id, plan_id)
        status = record.get("status")
        if status == PLAN_APPLIED:
            return {"ok": True, "applied": False, "already_applied": True, "plan": record}
        if status not in {PLAN_PENDING, PLAN_APPLYING}:
            raise PlanCardError("plan_not_applicable", f"plan {plan_id!r} is {status}; nothing to apply")
        record["status"] = PLAN_APPLYING
        record["updated_at"] = _now()
        atomic_write_json(path, record)
        promoted = read_canonical_todos_if_promoted(runtime_root=runtime_root, goal_id=goal_id) is not None
        if promoted:
            ids = _apply_canonical_sequence(registry_path, goal_id, plan_id, record["plan"], runtime_root_arg)
        else:
            ids = _apply_legacy_batch(registry_path, runtime_root, goal_id, plan_id, record["plan"])
        record.update({
            "status": PLAN_APPLIED, "todo_id_map": ids, "applied_at": _now(), "updated_at": _now(),
            "apply_mode": "canonical_sequence" if promoted else "legacy_batch",
        })
        atomic_write_json(path, record)
    _event(runtime_root, goal_id, "plan_decided", record, decision="approve")
    return {"ok": True, "applied": True, "already_applied": False, "plan": record}


def plan_for_gate(runtime_root: Path, goal_id: str, todo_id: str) -> str | None:
    entry = read_gate_index(runtime_root, goal_id)["gates"].get(str(todo_id)) or {}
    if entry.get("kind") == GATE_KIND_PLAN_APPROVAL and entry.get("plan_id"):
        return str(entry["plan_id"])
    return None


def gate_decision_preflight(
    *, registry_path: Path, runtime_root: Path, goal_id: str, todo_id: str, decision: str | None,
) -> str | None:
    """Validate the linked plan before an approve closes its gate.

    Returns the linked plan id (or None). An approve against a plan that no
    longer validates raises, so the gate stays open for discussion.
    """

    plan_id = plan_for_gate(runtime_root, goal_id, todo_id)
    if plan_id is None:
        return None
    record = read_plan(runtime_root, goal_id, plan_id)
    if decision == "approve" and record.get("status") in {PLAN_PENDING, PLAN_APPLYING}:
        _dry_run_validate(registry_path, goal_id, plan_id, record["plan"])
    return plan_id


def settle_gate_decision(
    *, registry_path: Path, runtime_root: Path, goal_id: str, todo_id: str, decision: str | None,
    runtime_root_arg: str | None = None,
) -> dict[str, Any] | None:
    """After a gate closed: apply (approve) or close (reject/cancel) its plan."""

    from .gate_threads import mark_gate_closed

    index_entry = read_gate_index(runtime_root, goal_id)["gates"].get(str(todo_id))
    if index_entry is not None:
        mark_gate_closed(runtime_root, goal_id, todo_id, decision=decision)
    plan_id = plan_for_gate(runtime_root, goal_id, todo_id)
    if plan_id is None:
        return None
    if decision == "approve":
        try:
            result = apply_plan(
                registry_path=registry_path, runtime_root=runtime_root, goal_id=goal_id,
                plan_id=plan_id, runtime_root_arg=runtime_root_arg,
            )
        except Exception as error:  # the gate is already closed; keep the plan retryable
            return {
                "ok": False, "plan_id": plan_id, "status": PLAN_APPLYING, "error": str(error),
                "recovery": f"loopx plan apply --goal-id {goal_id} --plan-id {plan_id}",
            }
        plan = result["plan"]
        return {"ok": True, "plan_id": plan_id, "status": plan["status"], "todo_id_map": plan["todo_id_map"]}
    path = plan_path(runtime_root, goal_id, plan_id)
    with exclusive_file_lock(path):
        record = read_plan(runtime_root, goal_id, plan_id)
        if record.get("status") == PLAN_PENDING:
            record["status"] = PLAN_REJECTED if decision == "reject" else PLAN_CANCELLED
            record["decided_at"] = record["updated_at"] = _now()
            atomic_write_json(path, record)
            _event(runtime_root, goal_id, "plan_decided", record, decision=str(decision))
    return {"ok": True, "plan_id": plan_id, "status": record.get("status"), "todo_id_map": {}}


def render_plan_markdown(payload: Mapping[str, Any]) -> str:
    if not payload.get("ok"):
        return f"plan: error: {payload.get('error')}\n"
    if "plans" in payload:
        lines = [f"# Plans for {payload.get('goal_id')}", ""]
        for plan in payload["plans"]:
            lines.append(
                f"- `{plan['plan_id']}` {plan['status']} rev {plan.get('revision')}: "
                f"{plan['plan']['title']} (gate `{plan.get('gate_todo_id')}`)"
            )
        if len(lines) == 2:
            lines.append("- none")
        return "\n".join(lines) + "\n"
    plan = payload.get("plan") or {}
    body = plan.get("plan") or {}
    lines = [
        f"# Plan `{plan.get('plan_id')}`: {body.get('title')}",
        "",
        f"- status: {plan.get('status')} (revision {plan.get('revision')})",
        f"- gate: `{plan.get('gate_todo_id')}`",
        f"- proposed by: {plan.get('proposed_by')}",
    ]
    if body.get("summary"):
        lines += ["", body["summary"]]
    lines += ["", "| key | todo | role | agent | acceptor | depends on | repos | estimate | todo id |", "|---|---|---|---|---|---|---|---|---|"]
    mapping = plan.get("todo_id_map") or {}
    for todo in body.get("todos") or []:
        lines.append(
            "| {key} | {text} | {role} | {agent} | {acc} | {deps} | {repos} | {est} | {tid} |".format(
                key=todo["key"], text=todo["text"], role=todo["required_role"],
                agent=todo.get("bound_agent") or "", acc=todo.get("acceptor_agent") or "",
                deps=", ".join(todo.get("depends_on") or []), repos=", ".join(todo.get("task_repositories") or []),
                est=todo.get("estimated_effort") or "", tid=mapping.get(todo["key"], ""),
            )
        )
    for todo in body.get("todos") or []:
        if todo.get("acceptance") or todo.get("validation_command"):
            lines.append(
                f"\n- `{todo['key']}` acceptance: {todo.get('acceptance') or '-'}; "
                f"validation: `{todo.get('validation_command') or '-'}`"
            )
    return "\n".join(lines) + "\n"


# --- dependency resume -----------------------------------------------------------


def resume_ready_plan_todos(
    *, registry_path: Path, goal_id: str, runtime_root: Path, runtime_root_arg: str | None = None,
) -> list[str]:
    """Reopen deferred plan todos whose plan dependencies are all ``done``.

    Plan apply creates a dependent todo ``deferred`` with one ``resume_when``
    condition (its last listed dependency). Nothing reopened it once that
    condition held, and a todo with several dependencies could only name one.
    This reads the full ``depends_on`` list from each applied plan card and
    reopens the todo through the ordinary Todo update, attributed to its
    claim owner (else the plan's orchestrator). Idempotent: an open or finished todo is left alone.
    """

    from .todos import list_goal_todos, update_goal_todo

    plans = [plan for plan in list_plans(runtime_root, goal_id) if plan.get("status") == "applied"]
    if not plans:
        return []
    listed = list_goal_todos(registry_path=registry_path, goal_id=goal_id, runtime_root_arg=runtime_root_arg)
    rows = {str(row.get("todo_id")): row for row in listed.get("todos") or [] if isinstance(row, Mapping)}
    resumed: list[str] = []
    for plan in plans:
        ids = plan.get("todo_id_map") if isinstance(plan.get("todo_id_map"), Mapping) else {}
        body = plan.get("plan") if isinstance(plan.get("plan"), Mapping) else {}
        for item in body.get("todos") or []:
            if not isinstance(item, Mapping):
                continue
            todo_id = str(ids.get(item.get("key")) or "")
            row = rows.get(todo_id)
            if row is None or row.get("status") != "deferred":
                continue
            deps = [str(ids.get(key) or "") for key in item.get("depends_on") or []]
            if not deps or not all(rows.get(dep, {}).get("status") == "done" for dep in deps):
                continue
            result = update_goal_todo(
                registry_path=registry_path, goal_id=goal_id, todo_id=todo_id, role="agent",
                runtime_root_arg=runtime_root_arg, status="open", clear_resume_when=True,
                # The claim owner, or for an unclaimed todo the proposing orchestrator.
                agent_id=row.get("claimed_by") or plan.get("proposed_by") or None,
            )
            if result.get("ok", True):
                resumed.append(todo_id)
    return resumed
