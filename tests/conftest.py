from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loopx.canary.runner import SMOKE_SUITE_CHOICES  # noqa: E402


def pytest_addoption(parser) -> None:
    group = parser.getgroup("loopx-smoke-suite")
    group.addoption(
        "--loopx-smoke-suite",
        "--smoke-suite",
        dest="loopx_smoke_suite",
        choices=sorted(SMOKE_SUITE_CHOICES),
        default=None,
        help=(
            "Opt in to the canary smoke-suite facade and pass this selector "
            "through to the LoopX runner."
        ),
    )
    group.addoption(
        "--loopx-smoke-module",
        "--smoke-module",
        action="append",
        default=[],
        dest="loopx_smoke_modules",
        help="Module token filter passed through to the LoopX runner. Repeat or comma-separate.",
    )
    group.addoption(
        "--loopx-smoke-exclude-module",
        "--smoke-exclude-module",
        action="append",
        default=[],
        dest="loopx_smoke_exclude_modules",
        help="Module token exclusion passed through to the LoopX runner. Repeat or comma-separate.",
    )
    group.addoption(
        "--loopx-smoke-script",
        "--smoke-script",
        action="append",
        default=[],
        dest="loopx_smoke_scripts",
        help="examples/**/*-smoke.py selector passed through to the LoopX runner. Repeat or comma-separate.",
    )
    group.addoption(
        "--loopx-smoke-profile",
        "--smoke-profile",
        action="append",
        default=[],
        dest="loopx_smoke_profiles",
        help=(
            "Smoke-suite or catalog profile selector passed through to the LoopX "
            "runner. Repeat or comma-separate."
        ),
    )
    group.addoption(
        "--loopx-smoke-family",
        "--smoke-family",
        action="append",
        default=[],
        dest="loopx_smoke_families",
        help="Catalog family selector passed through to the LoopX runner. Repeat or comma-separate.",
    )
    group.addoption(
        "--loopx-smoke-include-deep-checks",
        "--smoke-include-deep-checks",
        action="store_true",
        default=False,
        dest="loopx_smoke_include_deep_checks",
        help="Include deep catalog checks when profile or family selectors are used.",
    )
    group.addoption(
        "--loopx-smoke-limit",
        "--smoke-limit",
        type=int,
        default=0,
        dest="loopx_smoke_limit",
        help="Maximum selected checks to run. Defaults to all selected checks.",
    )
    group.addoption(
        "--loopx-smoke-offset",
        "--smoke-offset",
        type=int,
        default=0,
        dest="loopx_smoke_offset",
        help="Skip this many matched checks before applying the smoke-suite limit.",
    )
    group.addoption(
        "--loopx-smoke-timeout",
        "--smoke-timeout",
        type=float,
        default=120.0,
        dest="loopx_smoke_timeout",
        help="Per-check timeout in seconds for each subprocess smoke.",
    )


@pytest.fixture(autouse=True)
def _outside_agent_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run tests as the owner even when the suite itself runs inside an agent Turn.

    A LoopX Turn host marks its model process with ``LOOPX_AGENT_TURN``, and
    user-gate decisions refuse it. Tests that exercise that guard set it.
    """

    monkeypatch.delenv("LOOPX_AGENT_TURN", raising=False)


_GIT_ENV_OVERRIDES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)


@pytest.fixture
def independent_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A linked git worktree of a private temporary repository.

    A multi-agent goal only settles accountable delivery that was refreshed
    from an independent git worktree. Without an explicit delivery workspace,
    refresh-state and ``turn run-once`` capture the process cwd, so a test that
    relies on the default would pass from a linked worktree of the LoopX
    checkout and fail from the primary checkout. Tests use this worktree as
    their delivery workspace instead: pass it with --delivery-workspace-path,
    or ``monkeypatch.chdir`` into it the way the dispatcher starts a Turn in
    its workspace. Inherited ``GIT_*`` repository overrides (for example from
    a git hook) are cleared so git discovery only sees the temporary paths.
    """

    for name in _GIT_ENV_OVERRIDES:
        monkeypatch.delenv(name, raising=False)
    origin = tmp_path / "delivery-origin"
    worktree = tmp_path / "delivery-worktree"
    git = [
        "git",
        "-c", "user.name=LoopX Test",
        "-c", "user.email=loopx-test@example.invalid",
        "-c", "commit.gpgsign=false",
        "-c", "init.defaultBranch=main",
    ]
    subprocess.run([*git, "init", "--quiet", str(origin)], check=True)
    subprocess.run(
        [*git, "-C", str(origin), "commit", "--quiet", "--allow-empty", "-m", "init"],
        check=True,
    )
    subprocess.run(
        [*git, "-C", str(origin), "worktree", "add", "--quiet", "--detach", str(worktree)],
        check=True,
    )
    return worktree
