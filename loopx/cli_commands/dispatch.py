"""`loopx dispatch`: the resident role_v1 Turn dispatcher.

``serve`` watches the goals' state files and runs a periodic reconcile tick;
``serve --once`` runs a single pass. ``status`` reads the dispatcher's private
bookkeeping. ``launchd-plist`` prints (never installs) a launchd job.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from ..dispatch import (
    DispatchConfig,
    Dispatcher,
    DispatchLockError,
    dispatch_status,
    render_launchd_plist,
)
from ..dispatch.serve_heartbeat import DEFAULT_IDLE_HEARTBEAT_SECONDS, IdleHeartbeat, pass_is_loggable


def register_dispatch(subparsers, add_format) -> None:
    parser = subparsers.add_parser(
        "dispatch",
        help="Resident dispatcher that launches role_v1 agent Turns (serve, status, launchd-plist).",
    )
    actions = parser.add_subparsers(dest="dispatch_command", required=True)

    def add_serve_options(sub, *, for_plist: bool) -> None:
        sub.add_argument("--goal-id", dest="goal_ids", action="append", required=True)
        sub.add_argument("--project", help="Project root for agent files (.loopx/agents) and default turn cwd.")
        sub.add_argument("--tick-seconds", type=float, default=60.0)
        sub.add_argument("--poll-seconds", type=float, default=3.0, help="State-file watch interval.")
        sub.add_argument("--max-global", type=int, default=4, help="Machine-wide cap on concurrent Turns.")
        sub.add_argument("--turn-timeout-seconds", type=float, default=3600.0)
        sub.add_argument(
            "--long-cooldown-seconds",
            type=float,
            default=3600.0,
            help="A provider cooldown this long opens one user gate.",
        )
        sub.add_argument("--backoff-base-seconds", type=float, default=60.0)
        sub.add_argument(
            "--idle-heartbeat-seconds",
            type=float,
            default=DEFAULT_IDLE_HEARTBEAT_SECONDS,
            help="Print one idle line when no pass was printed for this long (0 disables).",
        )
        sub.add_argument("--no-global-sync", action="store_true", help="Pass --no-global-sync to every Turn.")
        sub.add_argument(
            "--validation-command-json",
            help=(
                "JSON argv used as the Turn's independent validator when the selected todo "
                "declares no validation command of its own."
            ),
        )
        if not for_plist:
            sub.add_argument("--once", action="store_true", help="Run one reconcile pass, wait for its Turns, exit.")

    serve = actions.add_parser("serve", help="Run the dispatcher (event watch + periodic reconcile).")
    add_format(serve)
    add_serve_options(serve, for_plist=False)
    status = actions.add_parser("status", help="Running Turns, cooldowns and per-agent slots.")
    add_format(status)
    plist = actions.add_parser("launchd-plist", help="Print a launchd plist for `dispatch serve` (not installed).")
    add_serve_options(plist, for_plist=True)
    plist.add_argument("--label")


def _config(args, registry_path: Path, runtime_root: Path) -> DispatchConfig:
    return DispatchConfig(
        registry_path=Path(registry_path),
        runtime_root=Path(runtime_root),
        goal_ids=list(dict.fromkeys(args.goal_ids)),
        project=Path(args.project).expanduser().resolve() if args.project else None,
        tick_seconds=max(1.0, args.tick_seconds),
        poll_seconds=max(0.2, args.poll_seconds),
        max_global=max(1, args.max_global),
        turn_timeout_seconds=max(30.0, args.turn_timeout_seconds),
        long_cooldown_seconds=max(1.0, args.long_cooldown_seconds),
        backoff_base_seconds=max(1.0, args.backoff_base_seconds),
        no_global_sync=bool(args.no_global_sync),
        default_validation_argv=_validation_argv(args.validation_command_json),
    )


def _validation_argv(raw: str | None) -> tuple[str, ...] | None:
    if not raw:
        return None
    value = json.loads(raw)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError("--validation-command-json must be a non-empty JSON string array")
    return tuple(value)


def _serve_args(args) -> list[str]:
    argv: list[str] = []
    for goal_id in dict.fromkeys(args.goal_ids):
        argv.extend(["--goal-id", goal_id])
    if args.project:
        argv.extend(["--project", str(Path(args.project).expanduser().resolve())])
    argv.extend(
        [
            "--tick-seconds", str(args.tick_seconds),
            "--poll-seconds", str(args.poll_seconds),
            "--max-global", str(args.max_global),
            "--turn-timeout-seconds", str(args.turn_timeout_seconds),
            "--long-cooldown-seconds", str(args.long_cooldown_seconds),
            "--backoff-base-seconds", str(args.backoff_base_seconds),
            "--idle-heartbeat-seconds", str(args.idle_heartbeat_seconds),
        ]
    )
    if args.no_global_sync:
        argv.append("--no-global-sync")
    if args.validation_command_json:
        argv.extend(["--validation-command-json", args.validation_command_json])
    return argv


def handle_dispatch(args, registry_path, runtime_root, print_payload, output_format) -> int:
    action = args.dispatch_command
    runtime_root = Path(runtime_root).expanduser()
    if action == "status":
        print_payload(dispatch_status(runtime_root), output_format(args), render_dispatch_status)
        return 0
    if action == "launchd-plist":
        sys.stdout.write(
            render_launchd_plist(
                registry_path=Path(registry_path),
                runtime_root=runtime_root,
                serve_args=_serve_args(args),
                environ=os.environ,
                label=args.label,
            )
        )
        return 0
    dispatcher = Dispatcher(_config(args, Path(registry_path), runtime_root))
    if args.once:
        try:
            report = dispatcher.run_once()
        except DispatchLockError as exc:
            print_payload({"ok": False, "error": "dispatcher_locked", "reason": str(exc)}, output_format(args), render_dispatch_pass)
            return 3
        report["ok"] = not report["errors"]
        print_payload(report, output_format(args), render_dispatch_pass)
        return 0 if report["ok"] else 1
    stopping = {"flag": False}

    def request_stop(_signum, _frame) -> None:
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    heartbeat = IdleHeartbeat(args.idle_heartbeat_seconds)

    def log_pass(report) -> None:
        if pass_is_loggable(report):
            print(json.dumps(report, ensure_ascii=False, default=str), flush=True)
        # Pilot v1 N11: an idle goal still shows a sign of life at a low rate.
        idle = heartbeat.observe(report, running_turns=len(dispatcher.state.get("runs") or {}))
        if idle is not None:
            print(json.dumps(idle, ensure_ascii=False, default=str), flush=True)

    try:
        dispatcher.serve(stop=lambda: stopping["flag"], on_pass=log_pass)
    except DispatchLockError as exc:
        print(json.dumps({"ok": False, "error": "dispatcher_locked", "reason": str(exc)}), file=sys.stderr)
        return 3
    return 0


def render_dispatch_pass(payload) -> str:
    if payload.get("error"):
        return f"dispatch: {payload['error']} — {payload.get('reason')}"
    lines = [f"Dispatch pass ({payload.get('trigger')}): launched {len(payload.get('launched') or [])}"]
    for item in payload.get("launched") or []:
        lines.append(f"- launched {item['agent_id']} on {item['goal_id']} todo {item.get('todo_id') or '-'} ({item.get('reason')})")
    for item in (payload.get("reaped") or []) + (payload.get("finished") or []):
        detail = ": ".join(str(item[key]) for key in ("error_code", "error") if item.get(key))
        lines.append(
            f"- finished {item['agent_id']} todo {item.get('todo_id') or '-'}: {item.get('outcome')} {item.get('failure_kind') or ''}".rstrip()
            + (f" — {detail}" if detail else "")
        )
    for item in payload.get("gates_opened") or []:
        lines.append(f"- opened user gate {item.get('todo_id')} ({item.get('key')})")
    for item in payload.get("skipped") or []:
        lines.append(f"- skip {item.get('agent_id')}: {item.get('reason')}")
    for item in payload.get("errors") or []:
        lines.append(f"- ERROR {item}")
    return "\n".join(lines)


def render_dispatch_status(payload) -> str:
    lines = [
        f"Dispatcher {'serving' if payload.get('serving') else 'not running'}"
        + (f" (pid {payload['lock_holder_pid']})" if payload.get("lock_holder_pid") else "")
    ]
    lines.append(f"Running turns: {len(payload.get('running') or [])}")
    for run in payload.get("running") or []:
        lines.append(
            f"- {run['agent_id']} ({run.get('role') or '-'}) goal {run['goal_id']} todo {run.get('todo_id') or '-'} "
            f"pid {run.get('pid')} {'alive' if run.get('alive') else 'exited'} {run.get('running_seconds')}s"
            + (" in the project directory" if run.get("workspace") == "project_directory" else "")
        )
    for item in (payload.get("last_pass") or {}).get("project_directory_waits") or []:
        # One project-directory Turn per goal (dispatch policy).
        lines.append(
            f"- {item.get('agent_id')} waits on todo {item.get('todo_id') or '-'} of {item.get('goal_id')}: "
            f"{item.get('reason')} (todo {item.get('running_todo_id') or '-'})"
        )
    for agent_id, slot in (payload.get("agent_slots") or {}).items():
        lines.append(f"- slots {agent_id} ({slot.get('role') or '-'}): {slot.get('running')}/{slot.get('max')}")
    for name, item in (payload.get("provider_cooldowns") or {}).items():
        lines.append(f"- provider {name} cooling down {item.get('remaining_seconds')}s ({item.get('kind')})")
    for name, item in (payload.get("agent_cooldowns") or {}).items():
        lines.append(f"- agent {name} unavailable {item.get('remaining_seconds')}s ({item.get('status')})")
    for key, item in (payload.get("todo_cooldowns") or {}).items():
        # Keyed goal/todo@agent (failure backoff) or goal/todo (every agent).
        scope, _, agent_id = key.partition("@")
        goal_id, _, todo_id = scope.partition("/")
        until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(item.get("until") or 0)))
        lines.append(
            f"- todo {todo_id} of {goal_id} ({agent_id or 'every agent'}) cooling down {item.get('remaining_seconds')}s "
            f"until {until}, failures {item.get('failures') or 0} ({item.get('reason')})"
            + (f": {item['error']}" if item.get("error") else "")
        )
    return "\n".join(lines)
