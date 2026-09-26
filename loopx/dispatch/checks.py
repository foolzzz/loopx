"""Independent Turn validators the dispatcher composes for orchestrator work.

``turn run-once`` requires an independent validator for material results. An
orchestrator todo that plans or resolves an escalation has no code to test,
so the dispatcher checks the goal state the orchestrator was asked to change.
Each check is read-only and exits 0 when satisfied, 1 otherwise.

    python -m loopx.dispatch.checks todo-not-status --registry R --runtime-root RT \
        --goal-id G --todo-id T --status blocked
"""

from __future__ import annotations

import argparse
import sys
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m loopx.dispatch.checks")
    sub = parser.add_subparsers(dest="check", required=True)
    not_status = sub.add_parser("todo-not-status", help="Pass when the todo exists and is not in --status.")
    for item in (not_status,):
        item.add_argument("--registry", required=True)
        item.add_argument("--runtime-root")
        item.add_argument("--goal-id", required=True)
        item.add_argument("--todo-id", required=True)
    not_status.add_argument("--status", action="append", required=True)
    args = parser.parse_args(argv)
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
