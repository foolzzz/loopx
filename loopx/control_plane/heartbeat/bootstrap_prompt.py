"""Stable host entrypoints; changing execution policy stays in task_body."""
from __future__ import annotations

import shlex
from pathlib import Path


BOOTSTRAP_INSTRUCTION = (
    "读取完整结果；仅 ok=true 时按本次 task_body 推进，不复用旧指令。"
    "一次操作不代表结束；通知与执行分开，等待按当前调度契约，不反复空查。"
    "入口异常先做权限内恢复；契约仍不可用时不执行任务或记账，并报告阻塞。"
)

LEGACY_HOST_BOOTSTRAP = "LoopX managed host bootstrap v1"
LEGACY_HOST_BOOTSTRAP_ENTRY = (
    "每次进入或恢复本 Goal 时先加载当前规则；升级后重新加载，"
    "不创建新 Goal、不接管宿主调度："
)


def render_bootstrap(command: list[str], *, title: str, entry: str) -> str:
    return f"{title}\n{entry}\n```sh\n{shlex.join(command)}\n```\n{BOOTSTRAP_INSTRUCTION}"


def _goal_command(args, *, registry: Path, legacy: bool) -> list[str]:
    """Preserve explicit caller inputs, not yesterday's resolved registry values.

    The loaded command deliberately omits --bootstrap: one load cannot recurse.
    A persistent entrypoint cannot pin an individual settlement identity.
    """
    if args.turn_instance_id:
        raise ValueError("--bootstrap cannot persist a --turn-instance-id; bind each work iteration through quota")
    if args.visible_goal_host == "traex-cli" and args.available_capabilities:
        raise ValueError("TraeX capability declarations require its separate host projection; use the direct Goal body")
    command = [args.cli_bin, "--format", "json", "--registry", str(registry.resolve())]
    if args.runtime_root:
        command += ["--runtime-root", str(Path(args.runtime_root).expanduser().resolve())]
    command += ["heartbeat-prompt"]
    mode = next((mode for mode in ("full", "compact", "brief", "thin") if getattr(args, mode)), "thin")
    # The v2 contract puts the wake-up mode and host identity immediately
    # after the subcommand. Native Goal loaders retain their historical contract.
    if not legacy:
        command.append("--" + mode)
    command += ["--goal-id", args.goal_id]
    for field, flag in (
        ("agent_id", "--agent-id"), ("active_state", "--active-state"),
        ("material_rule", "--material-rule"), ("permission_rule", "--permission-rule"),
        ("runtime_profile", "--runtime-profile"), ("visible_goal_host", "--visible-goal-host"),
        ("host_surface", "--host-surface"), ("scheduler_owner", "--scheduler-owner"),
        ("execution_mode", "--execution-mode"),
    ):
        value = getattr(args, field, None)
        if value is not None:
            if field == "active_state":
                value = str(Path(value).expanduser().resolve())
            command += [flag, value]
    for field, flag in (("agent_scopes", "--agent-scope"),
                        ("available_capabilities", "--available-capability")):
        for value in getattr(args, field, None) or []:
            command += [flag, value]
    if args.cli_bin != "loopx":
        command += ["--cli-bin", args.cli_bin]
    if legacy:
        command.append("--" + mode)
    return command


def goal_bootstrap(args, *, registry: Path) -> str:
    command = _goal_command(args, registry=registry, legacy=True)
    return render_bootstrap(command, title=LEGACY_HOST_BOOTSTRAP,
                            entry=LEGACY_HOST_BOOTSTRAP_ENTRY)
