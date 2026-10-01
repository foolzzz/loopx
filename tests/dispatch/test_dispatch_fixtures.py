from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.dispatch.dispatch_fixtures import git, make_repo


@pytest.mark.parametrize("config_variable", ["GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"])
def test_make_repo_ignores_ambient_git_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_variable: str,
) -> None:
    marker = tmp_path / "host-hook-ran"
    hooks = tmp_path / "host-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/bin/sh\nprintf ran > \"$HOST_HOOK_MARKER\"\nexit 97\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    monkeypatch.setenv("HOST_HOOK_MARKER", str(marker))

    reused_hooks = tmp_path / "repos" / ".loopx-test-hooks"
    reused_hooks.mkdir(parents=True)
    reused_hook = reused_hooks / "pre-commit"
    reused_hook.write_text(hook.read_text(encoding="utf-8"), encoding="utf-8")
    reused_hook.chmod(0o755)

    xdg_git = tmp_path / "host-xdg" / "git"
    xdg_git.mkdir(parents=True)
    (xdg_git / "ignore").write_text("README.md\n", encoding="utf-8")
    hostile_attributes = "README.md working-tree-encoding=definitely-invalid\n"
    (xdg_git / "attributes").write_text(hostile_attributes, encoding="utf-8")
    system_attributes = tmp_path / "host-system-attributes"
    system_attributes.write_text(hostile_attributes, encoding="utf-8")

    hostile_config = tmp_path / "host-gitconfig"
    hostile_config.write_text(
        "[commit]\n\tgpgSign = true\n\tcleanup = invalid\n"
        f"[core]\n\thooksPath = {hooks}\n",
        encoding="utf-8",
    )
    hostile_environment = {
        config_variable: str(hostile_config),
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "commit.gpgSign",
        "GIT_CONFIG_VALUE_0": "true",
        "GIT_CONFIG_KEY_1": "commit.cleanup",
        "GIT_CONFIG_VALUE_1": "invalid",
        "GIT_ALLOW_PROTOCOL": "https",
        "GIT_ATTR_SYSTEM": str(system_attributes),
        "GIT_DEFAULT_REF_FORMAT": "invalid",
        "GIT_DIR": str(tmp_path / "ambient.git"),
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_WORK_TREE": str(tmp_path / "ambient-worktree"),
        "XDG_CONFIG_HOME": str(xdg_git.parent),
    }
    monkeypatch.delenv("GIT_ATTR_NOSYSTEM", raising=False)
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    for name, value in hostile_environment.items():
        monkeypatch.setenv(name, value)

    repo = make_repo(tmp_path, "isolated")
    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", "--branch", "main", str(remote), str(clone))

    clean_env = {
        **{name: value for name, value in os.environ.items() if not name.startswith("GIT_")},
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repo,
        env=clean_env,
        check=True,
        capture_output=True,
        text=True,
    )
    readme = subprocess.run(
        ["git", "show", "HEAD:README.md"],
        cwd=repo,
        env=clean_env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert readme == "isolated\n"
    assert (clone / "README.md").read_text(encoding="utf-8") == "isolated\n"
    assert not marker.exists()
