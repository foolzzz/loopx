"""Acceptance-criteria changes through plan cards (fork design decision 40).

After initial planning, a change to a todo's ``acceptance_criteria`` takes
effect only through a user-approved plan card (decision 12 made real). The
orchestrator lists the change in the plan file it proposes::

    {"title": "Tighten the orders criteria",
     "criteria_changes": [{"todo_id": "todo_...", "new": "GET /orders pages by 50",
                           "reason": "the user asked for pagination"}]}

A card may carry only criteria changes, or batch them with new todos. It uses
the ordinary plan-card flow (propose, gate thread, approve / reject / cancel):

* **propose** reads each todo's current criteria into ``old``. A supplied
  ``old`` that no longer matches is refused as stale; a change that sets the
  current value, or a todo that already has a change on another pending card,
  is refused.
* **approve** sets each field (the lifecycle write is attributed like an
  owner edit) and appends a ``todo_criteria_change`` rollout event with the
  plan id. An entry whose todo's criteria changed after the proposal is
  refused as ``stale``; the other entries still apply.
* **reject / cancel** change nothing.
* After a reject, a cancel or a stale entry the orchestrator gets one
  orchestrator action todo, so the ordinary dispatcher path wakes it.

While a card with a change for a todo is pending, the acceptor does not
review that todo: role-aware selection and the dispatcher skip it (reason
``criteria_change_pending``). The developer keeps working; other todos
continue. Initial criteria (plan-card todos, ``todo add``) are unaffected.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .control_plane.todos.contract import TODO_ACCEPTANCE_CRITERIA_LIMIT, normalize_todo_acceptance_criteria

CRITERIA_CHANGE_KEY = "criteria_changes"
CRITERIA_CHANGE_EVENT_KIND = "todo_criteria_change"
CRITERIA_CHANGE_APPLIED = "applied"
CRITERIA_CHANGE_STALE = "stale"
CRITERIA_CHANGE_REFUSED = "refused"
CRITERIA_CHANGE_PENDING_REASON = "criteria_change_pending"
MAX_CRITERIA_CHANGES = 20
_CRITERIA_CHANGE_FIELDS = {"todo_id", "old", "new", "reason"}
_TODO_ID_PATTERN = re.compile(r"^todo_[A-Za-z0-9_-]{1,80}$")
# Plan statuses whose criteria changes are not decided yet.
_UNDECIDED_PLAN_STATUSES = frozenset({"pending", "applying"})
_CLOSED_TODO_STATUSES = frozenset({"done", "superseded", "cancelled"})


def _error(code: str, message: str) -> Exception:
    from .plan_cards import PlanCardError

    return PlanCardError(code, message)


def _criteria(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error("invalid_plan", f"{field} must be a string or null")
    text = " ".join(value.split())
    if len(text) > TODO_ACCEPTANCE_CRITERIA_LIMIT:
        raise _error("invalid_plan", f"{field} must be at most {TODO_ACCEPTANCE_CRITERIA_LIMIT} characters")
    return text or None


def normalize_criteria_changes(raw: Any) -> list[dict[str, Any]]:
    """Shape-check the plan file's ``criteria_changes`` (no state reads)."""

    if raw is None:
        return []
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_CRITERIA_CHANGES:
        raise _error("invalid_plan", f"criteria_changes must be a list of 1..{MAX_CRITERIA_CHANGES} items")
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        label = f"criteria_changes[{index}]"
        if not isinstance(item, dict):
            raise _error("invalid_plan", f"{label} must be an object")
        extra = sorted(set(item) - _CRITERIA_CHANGE_FIELDS)
        if extra:
            raise _error("invalid_plan", f"{label} has unknown field(s): {', '.join(extra)}")
        todo_id = str(item.get("todo_id") or "").strip()
        if not _TODO_ID_PATTERN.fullmatch(todo_id):
            raise _error("invalid_plan", f"{label}.todo_id must be an existing todo id")
        if todo_id in seen:
            raise _error("invalid_plan", f"duplicate criteria change for todo {todo_id!r}")
        if "new" not in item:
            raise _error("invalid_plan", f"{label}.new is required (null clears the criteria)")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise _error("invalid_plan", f"{label}.reason must be a non-empty string")
        change: dict[str, Any] = {
            "todo_id": todo_id,
            "new": _criteria(item.get("new"), f"{label}.new"),
            "reason": " ".join(reason.split())[:600],
        }
        if "old" in item:
            change["old"] = _criteria(item.get("old"), f"{label}.old")
        changes.append(change)
        seen.add(todo_id)
    return changes


def pending_criteria_changes(
    runtime_root: Path, goal_id: str, *, exclude_plan_id: str | None = None,
) -> dict[str, str]:
    """``{todo_id: plan_id}`` for criteria changes on undecided plan cards."""

    from .plan_cards import list_plans

    pending: dict[str, str] = {}
    for plan in list_plans(runtime_root, goal_id):
        if plan.get("status") not in _UNDECIDED_PLAN_STATUSES or plan.get("plan_id") == exclude_plan_id:
            continue
        body = plan.get("plan") if isinstance(plan.get("plan"), Mapping) else {}
        for change in body.get(CRITERIA_CHANGE_KEY) or []:
            if isinstance(change, Mapping) and change.get("todo_id"):
                pending.setdefault(str(change["todo_id"]), str(plan.get("plan_id")))
    return pending


def criteria_change_pending_todo_ids(runtime_root: Path | str | None, goal_id: str) -> list[str]:
    """Cheap read for selection and the dispatcher; never raises."""

    if not runtime_root:
        return []
    try:
        return sorted(pending_criteria_changes(Path(str(runtime_root)), goal_id))
    except (OSError, ValueError):
        return []


def _read_todo(registry_path: Path, goal_id: str, todo_id: str, runtime_root_arg: str | None) -> dict[str, Any] | None:
    from .todo_acceptance import _read_todo as read

    return read(registry_path=registry_path, goal_id=goal_id, todo_id=todo_id, runtime_root_arg=runtime_root_arg,
                project=None, state_file=None)


def bind_criteria_changes(
    *, registry_path: Path, runtime_root: Path, goal_id: str, plan: dict[str, Any],
    runtime_root_arg: str | None = None, exclude_plan_id: str | None = None,
) -> None:
    """Record each change's current criteria as ``old`` (propose/revise time)."""

    changes = plan.get(CRITERIA_CHANGE_KEY) or []
    if not changes:
        return
    pending = pending_criteria_changes(runtime_root, goal_id, exclude_plan_id=exclude_plan_id)
    for change in changes:
        todo_id = change["todo_id"]
        todo = _read_todo(registry_path, goal_id, todo_id, runtime_root_arg)
        if todo is None:
            raise _error("criteria_change_todo_not_found", f"criteria change names unknown todo {todo_id!r}")
        if str(todo.get("role") or "agent") != "agent":
            raise _error("invalid_plan", f"criteria change todo {todo_id!r} is not an agent todo")
        if str(todo.get("status") or "open") in _CLOSED_TODO_STATUSES:
            raise _error("invalid_plan", f"criteria change todo {todo_id!r} is already {todo.get('status')}")
        current = normalize_todo_acceptance_criteria(todo.get("acceptance_criteria"))
        if "old" in change and normalize_todo_acceptance_criteria(change["old"]) != current:
            raise _error(
                "criteria_change_stale",
                f"todo {todo_id!r} acceptance_criteria no longer match the proposal's old value; "
                "read them again (`loopx todo list`) and propose from the current criteria",
            )
        if normalize_todo_acceptance_criteria(change["new"]) == current:
            raise _error("invalid_plan", f"criteria change for todo {todo_id!r} does not change its criteria")
        if todo_id in pending:
            raise _error(
                "criteria_change_already_pending",
                f"todo {todo_id!r} already has a criteria change on pending plan {pending[todo_id]!r}; "
                "revise that plan instead",
            )
        change["old"] = current
        change["new"] = normalize_todo_acceptance_criteria(change["new"])


def _event(runtime_root: Path, goal_id: str, record: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    from .rollout_event_log import append_rollout_event, build_rollout_event, rollout_event_log_path

    event = build_rollout_event(
        goal_id=goal_id, event_kind=CRITERIA_CHANGE_EVENT_KIND, agent_id=record.get("proposed_by"),
        todo_id=str(result["todo_id"]), gate_id=record.get("gate_todo_id"), status=str(result["status"]),
        details={
            "plan_id": str(record["plan_id"]), "revision": int(record.get("revision") or 1),
            "approved_by": "user", "acceptance_criteria_change_class": "major",
            **{key: result.get(key) for key in ("previous_sha256", "sha256", "error") if result.get(key)},
        },
    )
    append_rollout_event(rollout_event_log_path(runtime_root, goal_id), event)


def apply_criteria_changes(
    *, registry_path: Path, runtime_root: Path, goal_id: str, record: dict[str, Any],
    persist: Callable[[dict[str, Any]], None], runtime_root_arg: str | None = None,
) -> list[dict[str, Any]]:
    """Apply an approved card's criteria changes, each at most once.

    Results are persisted after every entry, so an interrupted apply resumes
    without re-deciding a settled entry. A todo that already holds the new
    criteria (a write that landed before an interruption) counts as applied.
    """

    from .todo_acceptance_criteria import (
        ACCEPTANCE_CRITERIA_SOURCE_PLAN,
        StaleAcceptanceCriteriaError,
        acceptance_criteria_sha256,
        write_goal_todo_acceptance_criteria,
    )

    changes = (record.get("plan") or {}).get(CRITERIA_CHANGE_KEY) or []
    if not changes:
        return []
    settled = {str(item.get("todo_id")): item for item in record.get("criteria_change_results") or []}
    results: list[dict[str, Any]] = []
    for change in changes:
        todo_id = change["todo_id"]
        if todo_id in settled:
            results.append(settled[todo_id])
            continue
        result: dict[str, Any] = {
            "todo_id": todo_id,
            "previous_sha256": acceptance_criteria_sha256(change.get("old")),
            "sha256": acceptance_criteria_sha256(change.get("new")),
        }
        todo = _read_todo(registry_path, goal_id, todo_id, runtime_root_arg)
        current = normalize_todo_acceptance_criteria((todo or {}).get("acceptance_criteria"))
        if todo is not None and current == change.get("new") and current != change.get("old"):
            result["status"] = CRITERIA_CHANGE_APPLIED
        else:
            try:
                write_goal_todo_acceptance_criteria(
                    registry_path=registry_path, goal_id=goal_id, todo_id=todo_id,
                    acceptance_criteria=change.get("new"), author=None,
                    source=ACCEPTANCE_CRITERIA_SOURCE_PLAN, plan_id=str(record["plan_id"]),
                    expected_previous=change.get("old"), check_previous=True,
                    runtime_root_arg=runtime_root_arg,
                )
                result["status"] = CRITERIA_CHANGE_APPLIED
            except StaleAcceptanceCriteriaError as error:
                result.update(status=CRITERIA_CHANGE_STALE, error=str(error)[:300])
            except ValueError as error:  # the todo is gone or no longer writable
                result.update(status=CRITERIA_CHANGE_REFUSED, error=str(error)[:300])
        results.append(result)
        record["criteria_change_results"] = [*settled.values(), *[item for item in results if item["todo_id"] not in settled]]
        persist(record)
        _event(runtime_root, goal_id, record, result)
    return results


def notify_orchestrator(
    *, registry_path: Path, goal_id: str, record: Mapping[str, Any], decision: str,
    todo_ids: list[str], note: str | None = None, runtime_root_arg: str | None = None,
) -> str | None:
    """Wake the orchestrator after a criteria change did not take effect.

    One orchestrator action todo (the ordinary action-todo path: the
    dispatcher launches it and validates that the orchestrator changed some
    other todo, for example a revised plan card).
    """

    from .dispatch.orchestrator_actions import ORCHESTRATOR_ACTION_TEXT_PREFIX
    from .todos import add_goal_todo

    orchestrator = record.get("proposed_by")
    if not todo_ids or not orchestrator:
        return None
    what = ("was rejected" if decision == "reject" else "was cancelled" if decision == "cancel"
            else "was not applied (the todo changed after the proposal)")
    text = (
        f"{ORCHESTRATOR_ACTION_TEXT_PREFIX}the acceptance-criteria change of plan {record['plan_id']} {what}; "
        f"{', '.join(todo_ids)} keep their current acceptance criteria. Read the gate thread "
        f"(`loopx gate show --goal-id {goal_id} --todo-id {record.get('gate_todo_id')}`), then propose a revised "
        "plan card, update the todo (for example --review-feedback), or open a user gate."
    )
    added = add_goal_todo(
        registry_path=registry_path, goal_id=goal_id, role="agent", runtime_root_arg=runtime_root_arg,
        text=text[:600], task_class="advancement_task", action_kind="replan", claimed_by=str(orchestrator),
        role_contract={"required_role": "orchestrator", "requires_acceptance": False},
        note=(f"User note: {note}" if note else "Opened by LoopX after a plan-card decision.")[:600],
    )
    return str(added.get("todo_id") or "") or None


def criteria_changes_view(plan: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Old and new criteria side by side for ``gate show`` and the dashboard."""

    if not isinstance(plan, Mapping):
        return []
    body = plan.get("plan") if isinstance(plan.get("plan"), Mapping) else {}
    results = {str(item.get("todo_id")): item for item in plan.get("criteria_change_results") or []}
    rows = []
    for change in body.get(CRITERIA_CHANGE_KEY) or []:
        if not isinstance(change, Mapping):
            continue
        row = {key: change.get(key) for key in ("todo_id", "old", "new", "reason")}
        outcome = results.get(str(change.get("todo_id")))
        if outcome:
            row["result"] = outcome.get("status")
        rows.append(row)
    return rows


def plan_criteria_changes_view(runtime_root: Path, goal_id: str, plan_id: str) -> list[dict[str, Any]]:
    from .plan_cards import PlanCardError, read_plan

    try:
        return criteria_changes_view(read_plan(runtime_root, goal_id, plan_id))
    except (PlanCardError, OSError, ValueError):
        return []


def render_criteria_changes_markdown(rows: list[Mapping[str, Any]]) -> list[str]:
    """Markdown table of ``criteria_changes_view`` rows (``plan show`` / ``gate show``)."""

    if not rows:
        return []

    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|") if value else "_(none)_"

    lines = ["", "## Acceptance-criteria changes", "", "| todo | old criteria | new criteria | reason | result |",
             "|---|---|---|---|---|"]
    for row in rows:
        lines.append(
            f"| `{row['todo_id']}` | {cell(row.get('old'))} | {cell(row.get('new'))} | {cell(row.get('reason'))} "
            f"| {row.get('result') or ''} |"
        )
    return lines
