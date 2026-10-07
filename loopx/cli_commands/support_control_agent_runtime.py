from __future__ import annotations

import argparse


def register_agent_runtime_arguments(
    parser: argparse.ArgumentParser,
) -> None:
    """Register the shared Agent runtime selectors for Chat and Dashboard."""
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex CLI executable used for the read-only app-server session.",
    )
    parser.add_argument(
        "--claude-bin",
        default="claude",
        help="Claude Code CLI executable used for read-only Agent sessions.",
    )
    parser.add_argument(
        "--lark-cli-bin",
        help=(
            "Optional explicit lark-cli executable. When omitted, LoopX uses its "
            "bounded runtime discovery order."
        ),
    )
