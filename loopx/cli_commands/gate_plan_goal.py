"""`loopx gate`, `loopx plan` and `loopx goal` (fork slice S6).

- gate reply/show/list: user gate discussion threads (decision 10).
- plan propose/show/list/apply: orchestrator plan cards (decision 12).
- goal create: requirements-doc goal intake (decision 13).
- goal request-push: open the push_request user gate (G8, decision 18).
"""

from __future__ import annotations

import json
from pathlib import Path

GATE_PLAN_GOAL_COMMANDS = {"gate", "plan", "goal"}


def register_gate_plan_goal_commands(subparsers, add_format) -> None:
    gate = subparsers.add_parser("gate", help="Discuss and inspect user gates (discussion threads).")
    gate_actions = gate.add_subparsers(dest="gate_command", required=True)
    reply = gate_actions.add_parser("reply", help="Append a message to a user gate's discussion thread.")
    add_format(reply)
    reply.add_argument("--goal-id", required=True)
    reply.add_argument("--todo-id", required=True)
    reply.add_argument("--text", required=True)
    reply.add_argument(
        "--as", dest="author", choices=["user", "orchestrator"], default="user",
        help="user (owner, default) or orchestrator (requires --agent-id of the goal orchestrator).",
    )
    reply.add_argument("--agent-id", help="The goal orchestrator's agent id when replying --as orchestrator.")
    show = gate_actions.add_parser("show", help="Show a user gate's status and discussion thread.")
    add_format(show)
    show.add_argument("--goal-id", required=True)
    show.add_argument("--todo-id", required=True)
    resolve = gate_actions.add_parser(
        "resolve",
        help="Close a user gate with an owner decision; acceptor-blocked and budget_exhausted gates take an --option.",
    )
    add_format(resolve)
    resolve.add_argument("--goal-id", required=True)
    resolve.add_argument("--todo-id", required=True, help="The gate todo id.")
    resolve.add_argument("--decision", choices=["approve", "reject", "cancel"])
    resolve.add_argument(
        "--option",
        choices=["retry_acceptance", "accept_manually", "return_to_developer", "cancel_todo",
                 "raise_budget", "continue_without_limit", "stop_goal"],
        help="Acceptor-blocked gates (G12): retry_acceptance and accept_manually approve, "
             "return_to_developer rejects, cancel_todo cancels. Budget_exhausted gates (decision 41): "
             "raise_budget and continue_without_limit approve, stop_goal rejects.",
    )
    resolve.add_argument(
        "--note",
        help="Decision note; return_to_developer stores it as review_feedback, raise_budget reads the new "
             "USD budget from it (default +50%%).",
    )
    resolve.add_argument("--agent-id", help="Lifecycle actor; defaults to the agent the gate blocks.")
    resolve.add_argument("--dry-run", action="store_true")
    listing = gate_actions.add_parser("list", help="List open user gates with their thread state.")
    add_format(listing)
    listing.add_argument("--goal-id", required=True)
    listing.add_argument("--awaiting", choices=["user", "orchestrator"], help="Only gates awaiting this party.")

    plan = subparsers.add_parser("plan", help="Propose, inspect and apply orchestrator plan cards.")
    plan_actions = plan.add_subparsers(dest="plan_command", required=True)
    propose = plan_actions.add_parser(
        "propose", help="Store a pending plan and open a plan_approval user gate for it (orchestrator only).",
    )
    add_format(propose)
    propose.add_argument("--goal-id", required=True)
    propose.add_argument("--agent-id", required=True, help="The goal orchestrator.")
    propose.add_argument("--plan-file", required=True, help="Plan JSON (see docs/fork/gates-plans-intake-v0.md).")
    propose.add_argument("--revise", dest="revise_plan_id", help="Revise this pending plan in place (same gate).")
    plan_show = plan_actions.add_parser("show", help="Show one plan card.")
    add_format(plan_show)
    plan_show.add_argument("--goal-id", required=True)
    plan_show.add_argument("--plan-id", required=True)
    plan_list = plan_actions.add_parser("list", help="List the goal's plan cards.")
    add_format(plan_list)
    plan_list.add_argument("--goal-id", required=True)
    plan_list.add_argument(
        "--require-status",
        choices=["pending", "applying", "applied", "rejected", "cancelled"],
        help="Exit 1 unless at least one plan has this status (an orchestrator plan-todo validator).",
    )
    plan_apply = plan_actions.add_parser(
        "apply", help="Retry applying a plan whose gate was approved but whose apply was interrupted.",
    )
    add_format(plan_apply)
    plan_apply.add_argument("--goal-id", required=True)
    plan_apply.add_argument("--plan-id", required=True)

    goal = subparsers.add_parser("goal", help="Goal intake: create a role_v1 goal from a requirements doc.")
    goal_actions = goal.add_subparsers(dest="goal_command", required=True)
    create = goal_actions.add_parser(
        "create",
        help="Bootstrap a goal, register its requirements doc, agents and repos, and start the orchestrator.",
    )
    add_format(create)
    create.add_argument("--project", required=True, help="State home: project dir or central progress repo.")
    create.add_argument("--goal-id", required=True)
    create.add_argument("--doc", required=True, help="Requirements document (Markdown).")
    create.add_argument("--repo", dest="repos", action="append", default=[], help="NAME=PATH[,default_branch=B]... repeatable.")
    create.add_argument("--agent", dest="agents", action="append", default=[], help="AGENT_ID=ROLE, repeatable.")
    create.add_argument("--objective", help="Goal objective (defaults to the doc's first heading).")
    create.add_argument("--no-global-sync", action="store_true", help="Do not merge into the global registry.")
    create.add_argument("--dry-run", action="store_true")
    request_push = goal_actions.add_parser(
        "request-push",
        help="Open the goal's push_request user gate for its merged, unpushed work (G8).",
    )
    add_format(request_push)
    request_push.add_argument("--goal-id", required=True)
    request_push.add_argument(
        "--agent-id", help="The goal orchestrator when it asks; omit for the owner.",
    )
    request_push.add_argument("--dry-run", action="store_true")


def _format(args) -> str:
    from ..cli_runtime import output_format

    return output_format(args)


def _request_push(args, *, registry_path: Path, runtime_root_arg: str | None) -> dict:
    from ..agent_registry import load_goal_from_registry
    from ..gate_threads import require_goal_orchestrator
    from ..push_requests import request_push

    if args.agent_id:
        goal = load_goal_from_registry(registry_path, args.goal_id)
        if goal is None:
            raise ValueError(f"goal {args.goal_id!r} is not registered")
        require_goal_orchestrator(goal, args.agent_id)
    # The orchestrator's request respects a push the user declined until new
    # merges arrive; the owner may always ask again.
    return request_push(
        registry_path=registry_path, goal_id=args.goal_id, runtime_root_arg=runtime_root_arg,
        requested_by=args.agent_id or "owner", respect_declined=bool(args.agent_id), dry_run=args.dry_run,
    )


def handle_gate_plan_goal_command(
    args, *, registry_path: Path, registry_supplied: bool, runtime_root_arg: str | None, print_payload,
) -> int | None:
    if args.command not in GATE_PLAN_GOAL_COMMANDS:
        return None
    from ..control_plane.coordination.local_authority_shadow_adapter import effective_runtime_root
    from ..gate_threads import gate_view, list_gates, render_gate_markdown, reply_to_gate, resolve_gate
    from ..goal_intake import create_goal, render_goal_create_markdown
    from ..plan_cards import apply_plan, list_plans, propose_plan, read_plan, render_plan_markdown

    renderer = render_gate_markdown
    try:
        if args.command == "goal" and args.goal_command == "request-push":
            payload = _request_push(args, registry_path=registry_path, runtime_root_arg=runtime_root_arg)
        elif args.command == "goal":
            renderer = render_goal_create_markdown
            payload = create_goal(
                project=Path(args.project), goal_id=args.goal_id, doc=Path(args.doc),
                agents=args.agents, repos=args.repos,
                registry_path=registry_path if registry_supplied else None,
                runtime_root=Path(runtime_root_arg).expanduser() if runtime_root_arg else None,
                objective=args.objective, sync_global=not args.no_global_sync, dry_run=args.dry_run,
            )
        else:
            runtime_root = effective_runtime_root(registry_path, runtime_root_arg)
            if args.command == "gate":
                if args.gate_command == "reply":
                    payload = reply_to_gate(
                        registry_path=registry_path, runtime_root=runtime_root, goal_id=args.goal_id,
                        todo_id=args.todo_id, text=args.text, author=args.author, agent_id=args.agent_id,
                        runtime_root_arg=runtime_root_arg,
                    )
                elif args.gate_command == "resolve":
                    payload = resolve_gate(
                        registry_path=registry_path, goal_id=args.goal_id, todo_id=args.todo_id,
                        decision=args.decision, option=args.option, note=args.note,
                        agent_id=args.agent_id, runtime_root_arg=runtime_root_arg, dry_run=args.dry_run,
                    )
                elif args.gate_command == "show":
                    payload = gate_view(
                        registry_path=registry_path, runtime_root=runtime_root, goal_id=args.goal_id,
                        todo_id=args.todo_id, runtime_root_arg=runtime_root_arg,
                    )
                else:
                    payload = list_gates(
                        registry_path=registry_path, runtime_root=runtime_root, goal_id=args.goal_id,
                        awaiting=f"awaiting_{args.awaiting}" if args.awaiting else None,
                        runtime_root_arg=runtime_root_arg,
                    )
            else:
                renderer = render_plan_markdown
                if args.plan_command == "propose":
                    plan = json.loads(Path(args.plan_file).expanduser().read_text(encoding="utf-8"))
                    payload = propose_plan(
                        registry_path=registry_path, runtime_root=runtime_root, goal_id=args.goal_id,
                        agent_id=args.agent_id, plan=plan, revise_plan_id=args.revise_plan_id,
                        runtime_root_arg=runtime_root_arg,
                    )
                elif args.plan_command == "show":
                    payload = {"ok": True, "plan": read_plan(runtime_root, args.goal_id, args.plan_id)}
                elif args.plan_command == "list":
                    plans = list_plans(runtime_root, args.goal_id)
                    payload = {"ok": True, "goal_id": args.goal_id, "plans": plans}
                    required = getattr(args, "require_status", None)
                    if required and not any(plan.get("status") == required for plan in plans):
                        payload.update(
                            ok=False, error_code="plan_status_missing",
                            error=f"goal {args.goal_id!r} has no plan with status {required!r}",
                        )
                else:
                    payload = apply_plan(
                        registry_path=registry_path, runtime_root=runtime_root, goal_id=args.goal_id,
                        plan_id=args.plan_id, runtime_root_arg=runtime_root_arg,
                    )
    except (ValueError, OSError) as error:
        payload = {"ok": False, "error": str(error), "error_code": getattr(error, "code", None)}
    print_payload(payload, _format(args), renderer)
    return 0 if payload.get("ok") else 1

