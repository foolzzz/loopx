"""Render (never install) a launchd plist that keeps ``loopx dispatch serve`` running."""

from __future__ import annotations

import hashlib
import plistlib
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


def launchd_label(runtime_root: Path) -> str:
    digest = hashlib.sha256(str(Path(runtime_root).expanduser()).encode("utf-8")).hexdigest()[:10]
    return f"com.loopx.dispatch.{digest}"


def render_launchd_plist(
    *,
    registry_path: Path,
    runtime_root: Path,
    serve_args: Sequence[str],
    environ: Mapping[str, str],
    python: str | None = None,
    label: str | None = None,
) -> str:
    runtime_root = Path(runtime_root).expanduser()
    log_dir = runtime_root / "dispatch" / "logs"
    program = [
        python or sys.executable,
        "-m",
        "loopx.cli",
        "--registry",
        str(Path(registry_path).expanduser()),
        "--runtime-root",
        str(runtime_root),
        "dispatch",
        "serve",
        *serve_args,
    ]
    # launchd starts with a minimal PATH; the agent CLIs (claude, codex, git)
    # must be found the same way they are in the shell that rendered this.
    env = {"PATH": environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    if environ.get("HOME"):
        env["HOME"] = environ["HOME"]
    payload = {
        "Label": label or launchd_label(runtime_root),
        "ProgramArguments": program,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "WorkingDirectory": str(runtime_root),
        "EnvironmentVariables": env,
        "StandardOutPath": str(log_dir / "serve.out.log"),
        "StandardErrorPath": str(log_dir / "serve.err.log"),
    }
    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")
