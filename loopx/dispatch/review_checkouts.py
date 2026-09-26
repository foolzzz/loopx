"""Acceptor isolation for dispatched Turns (fork gap G12, design decision 35).

An acceptor Turn on a delivered (``in_review``) todo runs in a throwaway
detached checkout of the delivered commit, never in the developer's todo
worktree. After the Turn settles the dispatcher inspects the checkout (edits
or new commits raise an ``acceptor_modified_review_checkout`` warning event;
the verdict still stands) and removes it. While an ``acceptor_blocked`` user
gate is open for a todo, the acceptor is not relaunched on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..control_plane.todos.contract import TODO_STATUS_IN_REVIEW, normalize_todo_status

REVIEW_CHECKOUT_WARNING_EVENT = "acceptor_modified_review_checkout"


def is_review_turn(role: str | None, todo: Mapping[str, Any]) -> bool:
    return role == "acceptor" and normalize_todo_status(todo.get("status")) == TODO_STATUS_IN_REVIEW


def prepare_acceptor_review(
    goal: Mapping[str, Any], todo: Mapping[str, Any], runtime_root: Path, *, attempt: str,
) -> dict[str, Any]:
    """Create the review checkout for one acceptor Turn.

    The delivered shas come from the delivery evidence. A todo delivered
    before G12 has none recorded; its branch tips stand in for them.
    """

    from ..workspace.review_checkout import (
        delivered_branch_shas,
        parse_delivered_shas,
        prepare_review_checkout,
    )

    todo_id = str(todo.get("todo_id") or "")
    repos = [str(item) for item in (todo.get("task_repositories") or []) if item]
    delivered = parse_delivered_shas(todo.get("evidence"))
    source = "delivery_evidence"
    if not delivered:
        delivered = delivered_branch_shas(goal, todo_id, repos)
        source = "branch_tip_fallback"
    prepared = prepare_review_checkout(goal, todo_id, delivered, runtime_root, attempt=attempt)
    prepared["delivered_source"] = source
    if not prepared.get("ok"):
        # A partial checkout is removed right away; the Turn does not launch.
        remove_acceptor_review(goal, {"todo_id": todo_id, "attempt": attempt}, runtime_root)
        return prepared
    paths = dict(prepared.get("paths") or {})
    prepared["cwd"] = next(iter(paths.values())) if len(paths) == 1 else prepared["workspace_root"]
    return prepared


def review_run_record(prepared: Mapping[str, Any]) -> dict[str, Any]:
    """What the dispatcher state keeps to inspect and remove the checkout later."""

    return {
        "todo_id": prepared.get("todo_id"),
        "attempt": prepared.get("attempt"),
        "delivered_source": prepared.get("delivered_source"),
        "repos": [
            {key: item.get(key) for key in ("name", "ok", "path", "sha")}
            for item in prepared.get("repos") or [] if isinstance(item, Mapping)
        ],
    }


def remove_acceptor_review(goal: Mapping[str, Any], record: Mapping[str, Any], runtime_root: Path) -> dict[str, Any]:
    from ..workspace.review_checkout import remove_review_checkout

    try:
        return remove_review_checkout(
            goal, str(record.get("todo_id") or ""), runtime_root, attempt=str(record.get("attempt") or ""),
        )
    except Exception as error:  # noqa: BLE001 - removal is best-effort
        return {"ok": False, "error": str(error)[:300]}


def settle_acceptor_review(
    goal: Mapping[str, Any] | None, run: Mapping[str, Any], runtime_root: Path,
) -> dict[str, Any] | None:
    """Inspect, warn about and remove one finished Turn's review checkout."""

    record = run.get("review_checkout")
    if not isinstance(record, Mapping) or not isinstance(goal, Mapping):
        return None
    from ..workspace.review_checkout import inspect_review_checkout

    goal_id = str(goal.get("id") or "")
    try:
        inspection = inspect_review_checkout(goal, record)
    except Exception as error:  # noqa: BLE001 - inspection must never stop the reap
        inspection = {"modified": False, "repos": [], "error": str(error)[:300]}
    if inspection.get("modified"):
        append_review_warning(runtime_root, goal_id, run, inspection)
    removed = remove_acceptor_review(goal, record, runtime_root)
    return {"modified": bool(inspection.get("modified")), "removed": bool(removed.get("ok")),
            "repos": [item.get("name") for item in inspection.get("repos") or [] if item.get("modified")]}


def append_review_warning(
    runtime_root: Path, goal_id: str, run: Mapping[str, Any], inspection: Mapping[str, Any],
) -> None:
    """The verdict stands; the event log records that the acceptor changed code."""

    from ..rollout_event_log import append_rollout_event, build_rollout_event, rollout_event_log_path

    modified = [item for item in inspection.get("repos") or [] if item.get("modified")]
    try:
        append_rollout_event(
            rollout_event_log_path(runtime_root, goal_id),
            build_rollout_event(
                goal_id=goal_id, event_kind=REVIEW_CHECKOUT_WARNING_EVENT,
                agent_id=str(run.get("agent_id") or "") or None, todo_id=str(run.get("todo_id") or "") or None,
                run_id=str(run.get("turn_instance_id") or "") or None, agent_role="acceptor",
                status="warning",
                summary="The acceptor changed its review checkout; the change was discarded and the verdict stands.",
                details={
                    "modified_repos": [str(item.get("name")) for item in modified],
                    "uncommitted_file_count": sum(len(item.get("dirty_paths") or []) for item in modified),
                    "new_commit_repos": [str(item.get("name")) for item in modified if item.get("new_commits")],
                },
            ),
        )
    except (OSError, ValueError):
        pass


def blocked_review_todo_ids(registry_path: Path, runtime_root: Path, goal_id: str) -> set[str]:
    """Todos with an open ``acceptor_blocked`` gate: the acceptor waits on the user."""

    from ..todo_review_blocked import _open_user_todo_ids, open_review_gates

    try:
        open_ids = _open_user_todo_ids(registry_path, goal_id, str(runtime_root))
        return set(open_review_gates(runtime_root, goal_id, open_ids))
    except Exception:  # noqa: BLE001 - a failed read must not stop the pass
        return set()
