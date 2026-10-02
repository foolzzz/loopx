from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("command", ["global-gates", "global-todos", "global-risks"])
@pytest.mark.parametrize("output_format", ["json", "markdown"])
@pytest.mark.parametrize("explicit_runtime_root", [False, True])
@pytest.mark.parametrize("registry_kind", ["directory", "invalid-json"])
def test_invalid_registry_returns_public_safe_cli_error(
    tmp_path: Path,
    command: str,
    output_format: str,
    explicit_runtime_root: bool,
    registry_kind: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    registry = tmp_path / "private-registry"
    if registry_kind == "directory":
        registry.mkdir()
    else:
        registry.write_text("{", encoding="utf-8")
    argv = [sys.executable, "-m", "loopx.cli", "--registry", str(registry)]
    if explicit_runtime_root:
        argv.extend(["--runtime-root", str(tmp_path / "runtime")])
    argv.extend([command, "--format", output_format, "--scan-path", str(registry)])
    env = dict(os.environ, HOME=str(home), LOOPX_RUNTIME_ROOT=str(home / ".loopx"))

    proc = subprocess.run(
        argv,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 1
    assert proc.stderr == ""
    assert str(tmp_path) not in proc.stdout
    if output_format == "json":
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False
        assert payload["request"]["command"] == f"/loopx-{command}"
        assert payload["request"]["privacy_mode"] == "public_safe_summary"
        assert payload["error"]
        assert "summary" not in payload
    else:
        title = command.removeprefix("global-").title()
        assert f"# LoopX Global {title}" in proc.stdout
        assert "- ok: `False`" in proc.stdout
    if registry_kind == "directory":
        assert "<local-path-redacted>" in proc.stdout
