"""The one runtime-root resolver and the project goal-state location."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

import pytest

from loopx.cli_runtime import resolve_cli_registry
from loopx.paths import (
    RUNTIME_ROOT_ENV,
    SHELL_DEFAULT_RUNTIME_ROOT,
    configured_runtime_root,
    default_runtime_root,
    global_registry_path,
    home_runtime_root,
    project_goal_state_dir,
    project_goal_state_file,
    resolve_runtime_root,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(RUNTIME_ROOT_ENV, raising=False)
    return home


def test_the_default_runtime_root_is_dot_loopx_under_home(home: Path) -> None:
    assert configured_runtime_root() is None
    assert default_runtime_root() == home / ".loopx"
    assert home_runtime_root() == home / ".loopx"
    assert home_runtime_root(Path("/elsewhere")) == Path("/elsewhere/.loopx")
    assert global_registry_path() == home / ".loopx" / "registry.global.json"


def test_the_default_is_read_when_used_not_when_imported(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = default_runtime_root()
    later_home = tmp_path / "later-home"
    monkeypatch.setenv("HOME", str(later_home))

    assert before == home / ".loopx"
    assert default_runtime_root() == later_home / ".loopx"


def test_loopx_runtime_root_replaces_the_default(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured-root"
    monkeypatch.setenv(RUNTIME_ROOT_ENV, str(configured))

    assert configured_runtime_root() == configured
    assert default_runtime_root() == configured
    assert global_registry_path() == configured / "registry.global.json"


def test_a_relative_or_tilde_value_is_made_absolute(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(RUNTIME_ROOT_ENV, "state/../runtime")
    assert default_runtime_root() == tmp_path / "runtime"
    assert default_runtime_root().is_absolute()

    monkeypatch.setenv(RUNTIME_ROOT_ENV, "~/custom-root")
    assert default_runtime_root() == home / "custom-root"


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_value_configures_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(RUNTIME_ROOT_ENV, value)

    assert configured_runtime_root() is None
    assert default_runtime_root() == home / ".loopx"


def test_an_explicit_root_then_the_registry_root_win_over_the_default(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUNTIME_ROOT_ENV, str(tmp_path / "env-root"))
    registry = {"common_runtime_root": str(tmp_path / "registry-root")}

    assert resolve_runtime_root(registry, str(tmp_path / "explicit")) == tmp_path / "explicit"
    assert resolve_runtime_root(registry) == tmp_path / "registry-root"
    assert resolve_runtime_root({}) == tmp_path / "env-root"
    monkeypatch.delenv(RUNTIME_ROOT_ENV)
    assert resolve_runtime_root({}) == home / ".loopx"


def test_an_explicit_runtime_root_argument_wins_over_the_environment(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_root, explicit_root = tmp_path / "env-root", tmp_path / "explicit-root"
    for root in (env_root, explicit_root):
        root.mkdir()
        (root / "registry.global.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(RUNTIME_ROOT_ENV, str(env_root))
    monkeypatch.delenv("LOOPX_REGISTRY", raising=False)

    def fallback(runtime_root: str | None) -> Path:
        args = argparse.Namespace(
            command="status",
            registry=str(tmp_path / "missing" / "registry.json"),
            runtime_root=runtime_root,
        )
        return resolve_cli_registry(args, [])[0]

    assert fallback(None) == env_root / "registry.global.json"
    assert fallback(str(explicit_root)) == explicit_root / "registry.global.json"


@pytest.mark.parametrize("value", [None, "", "absolute", "relative/root"])
def test_the_shell_spelling_names_the_same_root_where_it_runs(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """A rendered command and a CLI started in the same directory agree."""

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    environ = {key: item for key, item in os.environ.items() if key != RUNTIME_ROOT_ENV}
    if value is not None:
        text = str(tmp_path / "configured-root") if value == "absolute" else value
        environ[RUNTIME_ROOT_ENV] = text
        monkeypatch.setenv(RUNTIME_ROOT_ENV, text)

    rendered = subprocess.run(
        ["sh", "-c", f'printf %s "{SHELL_DEFAULT_RUNTIME_ROOT}"'],
        env=environ,
        cwd=workdir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    # The shell leaves a relative value relative; the path it names from the
    # same working directory is the resolver's absolute root.
    assert Path(os.path.abspath(workdir / rendered)) == default_runtime_root()
    if not value:
        assert Path(rendered) == home / ".loopx"


def test_project_goal_state_lives_under_the_project_loopx_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"

    assert project_goal_state_dir(project, "goal-a") == project / ".loopx" / "goals" / "goal-a"
    assert project_goal_state_file(project, "goal-a") == (
        project / ".loopx" / "goals" / "goal-a" / "ACTIVE_GOAL_STATE.md"
    )
    assert project_goal_state_file(Path(), "goal-a").as_posix() == ".loopx/goals/goal-a/ACTIVE_GOAL_STATE.md"
