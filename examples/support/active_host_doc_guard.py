"""Guard active docs with an explicit retired App-host denylist."""

from __future__ import annotations

import re
from pathlib import Path


# Keep this list mechanical. It names removed command-line surfaces and runtime
# identifiers; it does not try to infer whether surrounding prose is historical,
# negative, or actionable.
RETIRED_COMMAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("--codex-app", re.compile(r"--codex-app(?![-\w])", re.IGNORECASE)),
    ("--trae-app", re.compile(r"--trae[-_]app(?![-\w])", re.IGNORECASE)),
    (
        "--host-surface <retired-app>",
        re.compile(
            r"--host-surface(?:\s+|=)[`\"']?(?:codex-app(?:-ssh)?|trae-app)(?![-\w])",
            re.IGNORECASE,
        ),
    ),
    (
        "--agent-type codex-app-ssh",
        re.compile(
            r"--agent-type(?:\s+|=)[`\"']?codex-app-ssh(?![-\w])",
            re.IGNORECASE,
        ),
    ),
    (
        "--runtime-profile <retired-app-profile>",
        re.compile(
            r"--runtime-profile(?:\s+|=)[`\"']?"
            r"(?:codex_app_heartbeat|codex_app_ssh_goal|trae_app)(?![-\w])",
            re.IGNORECASE,
        ),
    ),
    (
        "retired App runtime identifier",
        re.compile(
            r"\b(?:codex[-_]app[-_]ssh(?:[-_]goal)?|codex_app_heartbeat|trae_app)\b",
            re.IGNORECASE,
        ),
    ),
)


# These are exact operational phrases seen in stale onboarding guidance. Add a
# phrase only when an active document used it; broader natural-language policy
# belongs in review, not in this deterministic smoke.
RETIRED_INSTRUCTION_PHRASES: tuple[str, ...] = (
    "create a codex app heartbeat automation",
    "create codex app heartbeat automation",
    "configure a codex app heartbeat automation",
    "configure codex app heartbeat automation",
    "update a codex app heartbeat automation",
    "update codex app heartbeat automation",
    "enable a codex app heartbeat automation",
    "enable codex app heartbeat automation",
    "activate a codex app heartbeat automation",
    "activate codex app heartbeat automation",
    "create a trae app heartbeat automation",
    "create trae app heartbeat automation",
    "configure trae app automation",
    "update trae app automation",
    "enable trae app automation",
    "activate trae app automation",
    "app-hosted heartbeat workers should use automation_update",
    "在 codex app 中创建 heartbeat automation",
    "创建 codex app heartbeat automation",
    "配置 codex app heartbeat automation",
    "更新 codex app heartbeat automation",
    "启用 codex app heartbeat automation",
    "激活 codex app heartbeat automation",
    "在 trae app 中创建 heartbeat automation",
    "配置 trae app automation",
    "更新 trae app automation",
    "启用 trae app automation",
    "激活 trae app automation",
)


# This list owns current setup and host-selection guidance. Runtime scheduler
# references keep their own contract tests until those surfaces retire.
# Archives and changelogs outside the active Dev Book are out of scope.
ACTIVE_ONBOARDING_AND_HOST_INTEGRATION_PATHS = (
    "README.md",
    "README.zh-CN.md",
    "docs/README.md",
    "docs/guides/getting-started.md",
    "docs/guides/long-running-coding-agents.md",
    "docs/guides/newcomer-command-path.md",
    "docs/integration.md",
    "docs/heartbeat-automation-prompt.md",
    "docs/state-interaction-model.md",
    "docs/operations/new-project-codex-prompt.md",
    "docs/product/runtimes/codex-cli/codex-cli-automation-driver.md",
    "docs/product/runtimes/codex-cli/codex-cli-packaged-install.md",
    "docs/product/runtimes/codex-cli/codex-cli-tui-loop.md",
    "docs/product/runtimes/codex-cli/loopx-turn-codex-cli-quickstart.md",
    "docs/reference/protocols/host-integration-surface-v0.md",
    "docs/reference/protocols/loopx-goal-command-v0.md",
    "skills/loopx-project/SKILL.md",
)


def _normalized(text: str) -> str:
    without_shell_continuations = re.sub(r"\\\s+", " ", text)
    return " ".join(without_shell_continuations.split()).casefold()


def find_retired_app_host_instruction(text: str) -> str | None:
    normalized = _normalized(text)
    for label, pattern in RETIRED_COMMAND_PATTERNS:
        if pattern.search(normalized):
            return f"retired command: {label}"
    for phrase in RETIRED_INSTRUCTION_PHRASES:
        if phrase in normalized:
            return f"retired instruction phrase: {phrase}"
    return None


def assert_detector_examples() -> None:
    rejected = (
        "loopx heartbeat-prompt --bootstrap --thin --codex-app",
        "loopx heartbeat-prompt --thin --trae_app",
        "loopx agent-onboard --host-surface=trae-app",
        "loopx agent-onboard --agent-type codex-app-ssh",
        "loopx quota should-run --runtime-profile codex_app_ssh_goal",
        "loopx agent-onboard --host-surface " + "\\" + "\n codex-app",
        "codex_app_heartbeat",
        "Create a Codex App heartbeat automation for this project.",
        "Do not create a Codex App heartbeat automation.",
        "Configure Trae App automation for this project.",
        "app-hosted heartbeat workers should use automation_update.",
        "在 Trae App 中创建 heartbeat automation。",
    )
    allowed = (
        "Codex App onboarding and automation integration are retired.",
        "Do not restore old App automation or App-specific scheduler configuration.",
        "Use Codex CLI instead of the former App entrypoint.",
        "Create a generic CLI heartbeat automation.",
        "The historical App baseline is retained only as evidence.",
    )
    for example in rejected:
        assert find_retired_app_host_instruction(example) is not None, example
    for example in allowed:
        assert find_retired_app_host_instruction(example) is None, example


def assert_active_host_docs(repo_root: Path) -> None:
    assert_detector_examples()
    paths = {repo_root / path for path in ACTIVE_ONBOARDING_AND_HOST_INTEGRATION_PATHS}
    paths.update((repo_root / "docs" / "book").rglob("*.md"))
    for path in sorted(paths):
        text = path.read_text(encoding="utf-8")
        finding = find_retired_app_host_instruction(text)
        assert finding is None, (str(path.relative_to(repo_root)), finding)
