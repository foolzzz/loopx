"""Dispatcher-owned system prompt addendum for launched Turns.

Result fields are checked by the executor's public-safety validator, which
rejects local absolute paths. Claude Code in particular tends to echo absolute
paths in its summary, so every dispatched Turn is told to use repo-relative
paths. The Turn prompt itself (``codex_cli._prompt``, shared by both built-in
hosts) carries the same rule; this addendum adds the dispatch context.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from ..control_plane.turn_driver.codex_cli import RESULT_PATH_HYGIENE_INSTRUCTION

RESULT_PATH_HYGIENE_RULE = RESULT_PATH_HYGIENE_INSTRUCTION


def dispatch_prompt_addendum(
    *,
    goal_id: str,
    agent_id: str,
    role: str | None,
    todo_id: str | None,
    workspace_repos: Mapping[str, str] | None,
) -> str:
    lines = [
        "# LoopX dispatcher context",
        "",
        f"You are agent `{agent_id}` with role `{role or 'unassigned'}` on goal `{goal_id}`, "
        "running one headless LoopX Turn launched by the local dispatcher.",
        "",
        f"- {RESULT_PATH_HYGIENE_RULE}",
    ]
    if workspace_repos:
        names = sorted(workspace_repos)
        if len(names) == 1:
            lines.append(
                f"- Your working directory is the git worktree of repo `{names[0]}` for todo "
                f"`{todo_id}` on its own branch."
            )
        else:
            lines.append(
                f"- Your working directory holds one git worktree per repo for todo `{todo_id}`: "
                + ", ".join(f"`{name}/`" for name in names)
                + ". All of them share the same branch."
            )
        if role == "acceptor":
            lines.append(
                "- Review the todo's changes on this branch against its acceptance criteria. "
                "Do not modify, commit, push or merge anything."
            )
        else:
            lines.append(
                "- Commit your changes on the current branch in every repo you touch. "
                "Do not push, merge or switch branches; LoopX merges after acceptance."
            )
    return "\n".join(lines) + "\n"


def compose_system_prompt(
    *,
    destination: Path,
    base_prompt_file: str | None,
    addendum: str,
) -> Path:
    """Write the agent's own system prompt followed by the dispatcher addendum."""

    parts: list[str] = []
    if base_prompt_file:
        base = Path(base_prompt_file).expanduser()
        if base.is_file():
            parts.append(base.read_text(encoding="utf-8").rstrip() + "\n")
    parts.append(addendum)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(parts), encoding="utf-8")
    return destination


def replace_system_prompt_argument(argv: Sequence[str], prompt_file: Path) -> list[str]:
    """Swap (or add) ``--claude-system-prompt-file`` in claude-code host args."""

    result: list[str] = []
    skip = False
    for item in argv:
        if skip:
            skip = False
            continue
        if item == "--claude-system-prompt-file":
            skip = True
            continue
        if item.startswith("--claude-system-prompt-file="):
            continue
        result.append(item)
    result.extend(["--claude-system-prompt-file", str(prompt_file)])
    return result
