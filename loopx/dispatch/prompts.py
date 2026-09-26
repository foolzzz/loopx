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
    awaiting_gates: Sequence[str] | None = None,
    loopx_command: str = "loopx",
) -> str:
    """Render the dispatch context for one Turn.

    ``loopx_command`` is the exact CLI prefix (interpreter, registry and
    runtime root) the dispatcher itself uses. The orchestrator must run its
    gate and plan commands against the same state home; a bare ``loopx``
    would fall back to the default registry, which may be another state home.
    """

    lx = loopx_command or "loopx"
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
    if role == "acceptor" and todo_id:
        lines.append(
            f"- Todo `{todo_id}` was delivered for your review (status in_review). Verify it "
            "against its acceptance criteria and validation. Return validated_completion to "
            "accept it. To reject it, return repair_required with concrete feedback for the "
            "developer in summary; LoopX reopens it for the same developer, and a second "
            "rejection escalates to the orchestrator."
        )
    elif role == "developer" and todo_id:
        lines.append(
            "- If the todo carries review_feedback, an acceptor rejected an earlier delivery: "
            "address that feedback first. Return validated_completion when the work is done; "
            "LoopX then runs its validation and sends it to the acceptor."
        )
    if role == "orchestrator":
        lines += [
            f"- Run every LoopX command with this exact prefix so it reaches this goal's state "
            f"home: `{lx}`. It is shown below as `loopx`.",
            "- Talk to the user only through user gates. Open a question gate with `loopx todo add "
            f"--goal-id {goal_id} --role user --task-class user_gate --agent-id {agent_id} --text Q`, "
            "then read or answer its thread with `loopx gate show|reply --goal-id "
            f"{goal_id} --todo-id T --as orchestrator --agent-id {agent_id}`. Propose or revise "
            f"plans with `loopx plan propose --goal-id {goal_id} --agent-id {agent_id} --plan-file F "
            "[--revise PLAN_ID]`; the user approves, rejects or cancels the gate.",
            "- Your gate and plan commands are the orchestrator's own LoopX writes; they are allowed "
            "even though the Turn prompt says the adapter owns state writes.",
            "- When you are waiting on the user (you opened or answered a gate, or proposed a plan), "
            "return result_kind `user_action_required` and name the gate in summary. Return "
            "`validated_completion` for a planning todo only once its plan card is approved and "
            "applied; LoopX verifies that with `loopx plan list --require-status applied`.",
            "- An escalation todo names a todo the acceptor rejected twice (now blocked). Resolve "
            "it by reopening it with clearer instructions (`loopx todo update --goal-id "
            f"{goal_id} --todo-id T --status open --reject-count 0 --agent-id <its developer> "
            "--note ...`), reassigning, splitting or superseding it, or open a user gate. Then "
            "return validated_completion; LoopX checks that the todo is no longer blocked.",
        ]
        if awaiting_gates:
            lines.append(
                "- The user replied and is waiting for you on gate(s): "
                + ", ".join(f"`{gate}`" for gate in awaiting_gates)
                + ". Read each thread and answer, or propose a conclusion."
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
