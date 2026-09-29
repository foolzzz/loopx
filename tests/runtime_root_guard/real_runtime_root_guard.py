"""Refuse test access to the real user LoopX runtime root.

Tests must keep LoopX state under temporary directories. The owner's live
runtime root (``~/.codex/loopx``) may be in use by a running dispatcher while
the suite runs, so this guard never inspects that directory. It watches this
process's own filesystem operations through a Python audit hook and refuses the
ones that target a protected root, which covers every way LoopX computes the
default root (module constants, ``Path.home()``, ``~`` expansion).

The pytest session protects the runtime root of the HOME it started with, so a
test that points HOME at ``tmp_path`` still resolves and uses its own temporary
root. Child Python processes that inherit ``PYTHONPATH`` load the
``sitecustomize`` shim next to this module and install the same hook; they
append refusals to the report file named in the environment, which the pytest
fixture reads back to fail the test that spawned them.

This module is imported before pytest in child processes, so it must depend on
the standard library only.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

PROTECTED_ROOTS_ENV = "PYTEST_LOOPX_PROTECTED_RUNTIME_ROOTS"
REPORT_PATH_ENV = "PYTEST_LOOPX_RUNTIME_ROOT_GUARD_REPORT"

# Audit event -> positions of the path arguments it carries. Reads count too:
# a test that reads the owner's live state is not hermetic either.
_PATH_ARGUMENTS: dict[str, tuple[int, ...]] = {
    "open": (0,),
    "os.chdir": (0,),
    "os.chflags": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.link": (0, 1),
    "os.listdir": (0,),
    "os.lchflags": (0,),
    "os.lchmod": (0,),
    "os.mkdir": (0,),
    "os.removexattr": (0,),
    "os.remove": (0,),
    "os.rename": (0, 1),
    "os.rmdir": (0,),
    "os.scandir": (0,),
    "os.setxattr": (0,),
    "os.symlink": (0, 1),
    "os.truncate": (0,),
    "os.utime": (0,),
    "os.walk": (0,),
    "glob.glob": (0,),
    "glob.glob/2": (0, 2),
    "pathlib.Path.glob": (0,),
    "pathlib.Path.rglob": (0,),
    "shutil.copyfile": (0, 1),
    "shutil.copymode": (0, 1),
    "shutil.copystat": (0, 1),
    "shutil.copytree": (0, 1),
    "shutil.move": (0, 1),
    "shutil.rmtree": (0,),
    "sqlite3.connect": (0,),
    "tempfile.mkdtemp": (0,),
    "tempfile.mkstemp": (0,),
}
# Positions of the ``dir_fd`` arguments of os.* events; the events report -1
# when a path is not relative to a directory descriptor.
_DIR_FD_ARGUMENTS: dict[str, tuple[int, ...]] = {
    "os.chmod": (2,),
    "os.chown": (3,),
    "os.link": (2, 3),
    "os.mkdir": (2,),
    "os.remove": (1,),
    "os.rename": (2, 3),
    "os.rmdir": (1,),
    "os.symlink": (2,),
    "os.utime": (3,),
}

_protected: tuple[str, ...] = ()
_report_path: str | None = None
_report_cursor = 0
_installed = False
_reporting = False


def runtime_roots_for_home(home: str | os.PathLike[str]) -> list[str]:
    """Return the default LoopX runtime root for ``home`` in literal and real form."""

    root = os.path.join(os.path.abspath(os.fspath(home)), ".codex", "loopx")
    return sorted({os.path.normpath(root), os.path.realpath(root)})


def configure(protected_roots: list[str], report_path: str) -> None:
    """Protect ``protected_roots`` here and in child processes that inherit the env."""

    global _protected, _report_path, _report_cursor
    _protected = tuple(sorted({os.path.normpath(root) for root in protected_roots if root}))
    _report_path = report_path
    try:
        _report_cursor = os.stat(report_path).st_size
    except OSError:
        _report_cursor = 0
    os.environ[PROTECTED_ROOTS_ENV] = os.pathsep.join(_protected)
    os.environ[REPORT_PATH_ENV] = report_path


def install() -> None:
    """Install the audit hook once per process; audit hooks cannot be removed."""

    global _installed
    if not _installed:
        sys.addaudithook(_audit)
        _installed = True


def install_from_environment() -> None:
    """Child-process entry point used by the ``sitecustomize`` shim."""

    roots = [root for root in os.environ.get(PROTECTED_ROOTS_ENV, "").split(os.pathsep) if root]
    report_path = os.environ.get(REPORT_PATH_ENV)
    if roots and report_path:
        configure(roots, report_path)
        install()


def protected_roots() -> tuple[str, ...]:
    return _protected


def take_violations() -> list[dict[str, object]]:
    """Return refusals recorded by this process or its children since the last call."""

    global _report_cursor
    if not _report_path:
        return []
    try:
        with open(_report_path, "rb") as report:
            report.seek(_report_cursor)
            data = report.read()
    except FileNotFoundError:
        return []
    complete = data[: data.rfind(b"\n") + 1]
    _report_cursor += len(complete)
    violations = []
    for line in complete.decode("utf-8", "replace").splitlines():
        with contextlib.suppress(ValueError):
            violations.append(json.loads(line))
    return violations


def describe(nodeid: str, violations: list[dict[str, object]]) -> str:
    lines = [f"{nodeid} used the real LoopX runtime root instead of a temporary one:"]
    for violation in violations[:10]:
        origin = "this test process" if violation.get("pid") == os.getpid() else (
            f"child pid {violation.get('pid')}: {' '.join(map(str, violation.get('argv') or []))}"
        )
        lines.append(f"  - {violation.get('event')} {violation.get('path')} ({origin})")
    if len(violations) > 10:
        lines.append(f"  - ... {len(violations) - 10} more")
    lines.append(
        "Keep LoopX state under tmp_path: give the registry a temporary "
        "'common_runtime_root', pass --runtime-root / runtime_root, or run CLI "
        "subprocesses with HOME set to a temporary directory."
    )
    return "\n".join(lines)


@contextlib.contextmanager
def protecting(root: Path, report_path: Path) -> Iterator[None]:
    """Additionally protect ``root`` and record refusals in ``report_path`` only.

    Guard self-tests use a temporary stand-in for the real root so that a broken
    guard can never write the owner's runtime root.
    """

    global _report_cursor
    saved_roots, saved_report, saved_cursor = _protected, _report_path, _report_cursor
    configure([*saved_roots, str(root)], str(report_path))
    try:
        yield
    finally:
        configure(list(saved_roots), saved_report or "")
        _report_cursor = saved_cursor


def _path_text(value: object) -> str | None:
    if isinstance(value, (str, bytes)) or hasattr(value, "__fspath__"):
        try:
            return os.fsdecode(os.fspath(value))
        except TypeError:
            return None
    return None


def _protected_root_of(path: str) -> str | None:
    path = os.path.normpath(path)
    for root in _protected:
        if path == root or path.startswith(root + os.sep):
            return root
    return None


def _audit(event: str, args: tuple[object, ...]) -> None:
    positions = _PATH_ARGUMENTS.get(event)
    if positions is None or not _protected or _reporting:
        return
    relative_to_fd = any(
        position < len(args) and args[position] not in (None, -1)
        for position in _DIR_FD_ARGUMENTS.get(event, ())
    )
    for position in positions:
        path = _path_text(args[position]) if position < len(args) else None
        if path is None:
            continue
        if not os.path.isabs(path):
            # A relative symlink target resolves against the link, not the cwd.
            if relative_to_fd or (event == "os.symlink" and position == 0):
                continue
            try:
                path = os.path.join(os.getcwd(), path)
            except OSError:
                continue
        root = _protected_root_of(path)
        if root is not None:
            _refuse(event, path, root)


def _refuse(event: str, path: str, root: str) -> None:
    global _reporting
    _reporting = True
    try:
        record = {
            "event": event,
            "path": path,
            "root": root,
            "pid": os.getpid(),
            "argv": sys.argv[:4],
            "test": os.environ.get("PYTEST_CURRENT_TEST"),
        }
        if _report_path:
            with contextlib.suppress(OSError), open(_report_path, "a", encoding="utf-8") as report:
                report.write(json.dumps(record, default=str) + "\n")
    finally:
        _reporting = False
    raise PermissionError(
        errno.EACCES,
        f"test guard refused {event} under the real LoopX runtime root {root}",
        path,
    )
