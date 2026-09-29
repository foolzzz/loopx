"""Render (never install) a launchd plist that keeps ``loopx dispatch serve`` running."""

from __future__ import annotations

import hashlib
import os
import plistlib
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


def launchd_label(runtime_root: Path) -> str:
    digest = hashlib.sha256(str(Path(runtime_root).expanduser()).encode("utf-8")).hexdigest()[:10]
    return f"com.loopx.dispatch.{digest}"


def loopx_source_root() -> Path:
    """The directory that holds the ``loopx`` package this process runs."""

    import loopx

    return Path(loopx.__file__).resolve().parent.parent


def _pinned_pythonpath(source_root: Path, current: str | None) -> str:
    entries = [str(source_root)]
    for entry in (current or "").split(os.pathsep):
        if entry and os.path.normpath(entry) not in {os.path.normpath(item) for item in entries}:
            entries.append(entry)
    return os.pathsep.join(entries)


def render_launchd_plist(
    *,
    registry_path: Path,
    runtime_root: Path,
    serve_args: Sequence[str],
    environ: Mapping[str, str],
    python: str | None = None,
    label: str | None = None,
    source_root: Path | None = None,
) -> str:
    runtime_root = Path(runtime_root).expanduser()
    log_dir = runtime_root / "dispatch" / "logs"
    program = [
        python or sys.executable,
        "-m",
        "loopx.cli",
        "--registry",
        # launchd starts the job in the runtime root, not where this was rendered.
        str(Path(registry_path).expanduser().resolve()),
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
    # launchd passes only these variables, so pin the job to the LoopX that
    # rendered this plist (an installed release snapshot or a checkout), not
    # whatever the interpreter's site-packages resolves, such as an editable
    # install of another checkout. Turns the dispatcher starts inherit it.
    env["PYTHONPATH"] = _pinned_pythonpath(source_root or loopx_source_root(), environ.get("PYTHONPATH"))
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
