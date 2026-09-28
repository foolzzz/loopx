"""The launchd plist runs the LoopX code that rendered it.

launchd gives the job only the plist's environment. Without PYTHONPATH, the
job's `python -m loopx.cli` imported whatever the interpreter's site-packages
resolved, such as an editable install of a checkout, instead of the release
snapshot whose `loopx` rendered the plist.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import loopx
from loopx.dispatch.launchd import render_launchd_plist

SOURCE_ROOT = str(Path(loopx.__file__).resolve().parents[1])


def _render(tmp_path: Path, environ: dict[str, str], **kwargs) -> dict:
    text = render_launchd_plist(
        registry_path=tmp_path / "registry.json", runtime_root=tmp_path / "runtime",
        serve_args=["--goal-id", "g"], environ=environ, **kwargs,
    )
    return plistlib.loads(text.encode("utf-8"))


def test_the_plist_pins_pythonpath_to_the_rendering_loopx_source(tmp_path: Path) -> None:
    plist = _render(tmp_path, {"PATH": "/usr/bin", "HOME": str(tmp_path)})
    assert plist["EnvironmentVariables"]["PYTHONPATH"] == SOURCE_ROOT


def test_existing_pythonpath_entries_follow_the_source_root(tmp_path: Path) -> None:
    current = os.pathsep.join(["/opt/extra", SOURCE_ROOT, "", "/opt/extra"])
    plist = _render(tmp_path, {"PATH": "/usr/bin", "PYTHONPATH": current})
    assert plist["EnvironmentVariables"]["PYTHONPATH"].split(os.pathsep) == [SOURCE_ROOT, "/opt/extra"]


def test_the_plist_environment_imports_the_pinned_source(tmp_path: Path) -> None:
    # Run the plist's interpreter with only the plist's environment and working
    # directory, as launchd does: it must import the pinned source, ahead of
    # any editable install of this checkout in the interpreter's site-packages.
    pinned = tmp_path / "release"
    (pinned / "loopx").mkdir(parents=True)
    (pinned / "loopx" / "__init__.py").write_text("", encoding="utf-8")
    plist = _render(tmp_path, {"PATH": os.environ.get("PATH", "")}, source_root=pinned)
    Path(plist["WorkingDirectory"]).mkdir(parents=True)
    probe = subprocess.run(
        [plist["ProgramArguments"][0], "-c", "import loopx; print(loopx.__file__)"],
        env=dict(plist["EnvironmentVariables"]), cwd=plist["WorkingDirectory"],
        capture_output=True, text=True, check=True,
    )
    assert Path(probe.stdout.strip()).resolve() == (pinned / "loopx" / "__init__.py").resolve()
