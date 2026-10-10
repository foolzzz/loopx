"""``loopx serve``: auto-init + launch the dashboard in one step."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path


def register_serve_command(
    subparsers: argparse._SubParsersAction,
    add_subcommand_format: Callable[[argparse.ArgumentParser], None],
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "serve",
        help="Start LoopX (auto-init if needed, then launch the dashboard).",
    )
    add_subcommand_format(parser)
    parser.add_argument(
        "--host", default=None, help="Loopback bind host (default 127.0.0.1)."
    )
    parser.add_argument("--port", type=int, default=None, help="Dashboard port (default 8767).")
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Start the server without opening a browser.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Print HTTP request logs."
    )
    parser.add_argument(
        "--runtime-root",
        help="Override the default ~/.loopx state directory.",
    )
    parser.add_argument(
        "--force-init",
        action="store_true",
        help="Re-run init even if already configured.",
    )
    return parser


def handle_serve_command(args, _print_payload) -> int | None:
    if getattr(args, "command", None) != "serve":
        return None
    return _run_serve(args)


def _run_serve(args) -> int:
    from ..chat_server import DEFAULT_CHAT_HOST, DEFAULT_CHAT_PORT
    from ..paths import default_runtime_root

    runtime_root_override = getattr(args, "runtime_root", None)
    runtime_root = Path(runtime_root_override) if runtime_root_override else default_runtime_root()
    providers_path = runtime_root / "providers.yaml"
    agents_dir = runtime_root / "agents"

    # --- Auto-init if no configuration exists ---
    needs_init = (
        getattr(args, "force_init", False)
        or not providers_path.exists()
        or not agents_dir.exists()
        or not any(agents_dir.glob("*.yaml"))
    )

    if needs_init:
        from .init import _run_init

        rc = _run_init(
            non_interactive=True,
            force=getattr(args, "force_init", False),
            runtime_root_override=runtime_root_override,
        )
        if rc != 0:
            return rc

    # --- Launch dashboard ---
    from ..dashboard_launcher import launch_dashboard

    host = getattr(args, "host", None) or DEFAULT_CHAT_HOST
    port = getattr(args, "port", None) or DEFAULT_CHAT_PORT

    return launch_dashboard(
        runtime_root_override=Path(runtime_root_override) if runtime_root_override else None,
        host=host,
        port=port,
        verbose=getattr(args, "verbose", False),
        open_browser=not getattr(args, "no_open", False),
    )
