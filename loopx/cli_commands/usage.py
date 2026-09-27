"""`loopx usage`: Turn cost and token accounting (fork gap G9).

``report`` aggregates the per-goal usage ledgers; without ``--goal`` it covers
every registry goal (the per-person view, e.g. ``--by day``). ``budget`` sets,
clears or shows a goal's optional spend budget.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..usage_accounting import (
    USAGE_REPORT_GROUPINGS,
    budget_status,
    build_usage_report,
    parse_since,
    render_usage_report_text,
    write_usage_budget,
)


def register_usage_commands(subparsers, add_format) -> None:
    del add_format  # usage keeps its own json|text choice
    parser = subparsers.add_parser(
        "usage",
        help="Turn cost, tokens and agent-hours per role, agent, goal, todo, model or day.",
    )
    actions = parser.add_subparsers(dest="usage_command", required=True)

    def add_format(sub) -> None:
        sub.add_argument("--format", dest="subcommand_format", choices=["json", "text", "markdown"])

    report = actions.add_parser("report", help="Aggregate the usage ledgers.")
    add_format(report)
    report.add_argument("--goal", "--goal-id", dest="goal_ids", action="append",
                        help="Goal to report (repeatable). Default: every registry goal.")
    window = report.add_mutually_exclusive_group()
    window.add_argument("--since", help="Only Turns started at or after this ISO date/timestamp.")
    window.add_argument("--days", type=int, help="Only Turns from the last N days.")
    report.add_argument("--by", choices=USAGE_REPORT_GROUPINGS, help="Group the totals.")

    budget = actions.add_parser("budget", help="Show, set or clear a goal's spend budget (USD).")
    add_format(budget)
    budget.add_argument("--goal", "--goal-id", dest="goal_id", required=True)
    change = budget.add_mutually_exclusive_group()
    change.add_argument("--set", dest="budget_usd", type=float, help="Budget in USD.")
    change.add_argument("--clear", action="store_true")


def _render_budget(payload: dict[str, Any]) -> str:
    status = payload.get("status")
    if not status:
        return f"{payload['goal_id']}: no usage budget\n"
    return (
        f"{payload['goal_id']}: ${status['spent_usd']:,.2f} of ${status['budget_usd']:,.2f} "
        f"({status['spent_ratio'] * 100:.0f}%)\n"
    )


def handle_usage_command(args, *, registry_path: Path, runtime_root: Path, print_payload, output_format) -> int | None:
    if args.command != "usage":
        return None
    runtime_root = Path(runtime_root).expanduser()
    try:
        if args.usage_command == "report":
            payload = build_usage_report(
                registry_path=Path(registry_path),
                runtime_root=runtime_root,
                goal_ids=list(args.goal_ids or []),
                by=args.by,
                since=parse_since(args.since, days=args.days),
            )
            payload["ok"] = True
            print_payload(payload, output_format(args), render_usage_report_text)
            return 0
        if args.budget_usd is not None or args.clear:
            write_usage_budget(runtime_root, args.goal_id, None if args.clear else args.budget_usd)
        payload = {"ok": True, "goal_id": args.goal_id, "status": budget_status(runtime_root, args.goal_id)}
        print_payload(payload, output_format(args), _render_budget)
        return 0
    except ValueError as exc:
        print_payload({"ok": False, "error": str(exc)}, output_format(args), lambda p: f"usage: {p['error']}\n")
        return 2
