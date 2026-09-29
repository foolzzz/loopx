"""pytest integration of the real-runtime-root guard.

Importing this module starts the guard for the pytest process. tests/conftest.py
imports it before anything else and re-exports its fixture and hooks; a
separate pytest process can load it with ``-p real_runtime_root_guard_plugin``
when this directory is on ``sys.path``.

A refusal fails the test it was recorded under, at that test's teardown.
Refusals recorded under another test or under no test are reported once for
the whole session, which then exits with a failure status. Under xdist each
worker hands those refusals to the controller.
"""

from __future__ import annotations

import atexit
import os
import tempfile
from pathlib import Path

import pytest

import real_runtime_root_guard as guard

GUARD_DIR = Path(__file__).resolve().parent
_WORKER_OUTPUT_KEY = "loopx_runtime_root_unattributed"
_unattributed: list[dict[str, object]] = []


def _start() -> None:
    """Protect the real runtime roots for this process and its Python children.

    The roots are captured here, before any test can monkeypatch HOME: the
    default runtime root of the starting HOME and of the account's home
    directory, plus an ambient LOOPX_RUNTIME_ROOT. A nested pytest session
    inherits its parent's roots instead of protecting the temporary HOME it may
    run under.
    """

    inherited = os.environ.get(guard.PROTECTED_ROOTS_ENV)
    if inherited:
        roots = inherited.split(os.pathsep)
    else:
        homes = [Path.home()]
        try:
            import pwd

            homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
        except (ImportError, KeyError):
            pass
        roots = [guard.default_runtime_root(home) for home in homes]
        ambient = os.environ.get("LOOPX_RUNTIME_ROOT")
        if ambient:
            roots.append(os.path.abspath(os.path.expanduser(ambient)))
    handle, report_path = tempfile.mkstemp(prefix="loopx-runtime-root-guard-", suffix=".jsonl")
    os.close(handle)
    atexit.register(Path(report_path).unlink, missing_ok=True)
    guard.configure(roots, report_path)
    guard.install()
    os.environ["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(GUARD_DIR), os.environ.get("PYTHONPATH")) if part
    )


_start()


@pytest.fixture(autouse=True)
def _refuse_real_loopx_runtime_root(request: pytest.FixtureRequest):
    yield
    own, others = guard.take_violations(request.node.nodeid)
    _unattributed.extend(others)
    if own:
        pytest.fail(guard.describe(request.node.nodeid, own), pytrace=False)


def pytest_sessionfinish(session: pytest.Session) -> None:
    _unattributed.extend(guard.take_violations(None)[1])
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[_WORKER_OUTPUT_KEY] = list(_unattributed)
        return
    if _unattributed and session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error) -> None:
    _unattributed.extend(getattr(node, "workeroutput", {}).get(_WORKER_OUTPUT_KEY, []))


def pytest_terminal_summary(terminalreporter) -> None:
    if _unattributed and not hasattr(terminalreporter.config, "workeroutput"):
        terminalreporter.section("real LoopX runtime root", red=True, bold=True)
        terminalreporter.line(guard.describe_unattributed(_unattributed))
