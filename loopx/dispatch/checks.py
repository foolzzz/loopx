"""Independent Turn validators the dispatcher composes for orchestrator work.

``turn run-once`` requires an independent validator for material results. An
orchestrator todo that plans or resolves an escalation has no code to test,
so the dispatcher checks the goal state the orchestrator was asked to change.
Each check is read-only and exits 0 when satisfied, 1 otherwise.

    python -m loopx.dispatch.checks todo-not-status --registry R --runtime-root RT \
        --goal-id G --todo-id T --status blocked
    python -m loopx.dispatch.checks gates-not-awaiting --registry R --runtime-root RT \
        --goal-id G --gate-id T [--gate-id T2 ...]
    python -m loopx.dispatch.checks todos-changed-since --registry R --runtime-root RT \
        --goal-id G --exclude-todo-id T --since ISO8601
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path


def todo_status(registry: Path, runtime_root: str | None, goal_id: str, todo_id: str) -> str | None:
    from ..todos import list_goal_todos

    listed = list_goal_todos(
        registry_path=registry, goal_id=goal_id, todo_id=todo_id, runtime_root_arg=runtime_root,
    )
    for item in listed.get("todos") or []:
        if isinstance(item, dict) and item.get("todo_id") == todo_id:
            return str(item.get("status") or "")
    return None


def _gates_still_awaiting(registry: Path, runtime_root: str | None, goal_id: str, gate_ids: list[str]) -> list[str]:
    """Listed gates that are still open and whose thread awaits the orchestrator."""

    from ..control_plane.coordination.local_authority_shadow_adapter import effective_runtime_root
    from ..gate_threads import AWAITING_ORCHESTRATOR, read_gate_index

    index = read_gate_index(effective_runtime_root(registry, runtime_root), goal_id)["gates"]
    waiting: list[str] = []
    for gate_id in gate_ids:
        entry = index.get(gate_id)
        if not isinstance(entry, dict) or entry.get("awaiting") != AWAITING_ORCHESTRATOR:
            continue
        if todo_status(registry, runtime_root, goal_id, gate_id) == "open":
            waiting.append(gate_id)
    return waiting


def _todos_changed_since(
    registry: Path, runtime_root: str | None, goal_id: str, exclude: str, since: str,
) -> list[str]:
    from ..todos import list_goal_todos

    threshold = datetime.fromisoformat(since)
    listed = list_goal_todos(registry_path=registry, goal_id=goal_id, runtime_root_arg=runtime_root)
    changed: list[str] = []
    for item in listed.get("todos") or []:
        if not isinstance(item, dict) or item.get("todo_id") == exclude:
            continue
        try:
            updated = datetime.fromisoformat(str(item.get("updated_at") or ""))
        except ValueError:
            continue
        if updated.tzinfo is None or threshold.tzinfo is None:
            updated, threshold = updated.replace(tzinfo=None), threshold.replace(tzinfo=None)
        if updated >= threshold:
            changed.append(str(item.get("todo_id")))
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m loopx.dispatch.checks")
    sub = parser.add_subparsers(dest="check", required=True)
    not_status = sub.add_parser("todo-not-status", help="Pass when the todo exists and is not in --status.")
    gates = sub.add_parser("gates-not-awaiting", help="Pass when no listed gate is open and awaiting the orchestrator.")
    changed = sub.add_parser("todos-changed-since", help="Pass when another todo was created or changed since --since.")
    for item in (not_status, gates, changed):
        item.add_argument("--registry", required=True)
        item.add_argument("--runtime-root")
        item.add_argument("--goal-id", required=True)
    not_status.add_argument("--todo-id", required=True)
    not_status.add_argument("--status", action="append", required=True)
    gates.add_argument("--gate-id", action="append", required=True)
    changed.add_argument("--exclude-todo-id", required=True)
    changed.add_argument("--since", required=True)
    args = parser.parse_args(argv)
    if args.check == "gates-not-awaiting":
        waiting = _gates_still_awaiting(Path(args.registry), args.runtime_root, args.goal_id, list(args.gate_id))
        if waiting:
            print(f"gates still await the orchestrator: {', '.join(waiting)}", file=sys.stderr)
            return 1
        print("no listed gate awaits the orchestrator")
        return 0
    if args.check == "todos-changed-since":
        changed_ids = _todos_changed_since(
            Path(args.registry), args.runtime_root, args.goal_id, args.exclude_todo_id, args.since,
        )
        if not changed_ids:
            print(f"no other todo changed since {args.since}", file=sys.stderr)
            return 1
        print(f"changed since {args.since}: {', '.join(changed_ids[:10])}")
        return 0
    status = todo_status(Path(args.registry), args.runtime_root, args.goal_id, args.todo_id)
    if status is None:
        print(f"todo {args.todo_id} was not found", file=sys.stderr)
        return 1
    if status in set(args.status):
        print(f"todo {args.todo_id} is still {status}", file=sys.stderr)
        return 1
    print(f"todo {args.todo_id} is {status}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(main())
