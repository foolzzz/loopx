"""Installer failures must leave no writer alive when the smoke removes HOME."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX installer smoke")
SMOKE_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples/release/release-promotion-concurrency-smoke.py"
)


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("promotion_smoke", SMOKE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installers(tmp_path, monkeypatch, smoke):
    """Real process trees; the child survives TERM and closes inherited pipes."""
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(0.01)\n"
    )
    parent_script = tmp_path / "installer.py"
    parent_script.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "output = {} if os.environ.get('TEST_INHERIT_PIPES') else "
        "dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], **output)\n"
        "if os.environ['LOOPX_RELEASE_ID'] == 'empty-lock-recovery':\n"
        "    lock = pathlib.Path(os.environ['LOOPX_RELEASES_DIR']) / '.install-lock'\n"
        "    (lock / 'pid').write_text(str(os.getpid()))\n"
        "while True: time.sleep(0.01)\n"
    )
    real_popen = subprocess.Popen
    processes = []
    child_pids = []

    def start(_command, **kwargs):
        child_pid_file = tmp_path / f"child-{len(processes)}.pid"
        process = real_popen(
            [sys.executable, str(parent_script), str(child_script), str(child_pid_file)],
            **kwargs,
        )
        processes.append(process)
        deadline = time.monotonic() + 10
        while not child_pid_file.exists() or not child_pid_file.read_text():
            if time.monotonic() >= deadline:
                pytest.fail("synthetic installer did not start its child")
            time.sleep(0.01)
        child_pids.append(int(child_pid_file.read_text()))
        return process

    monkeypatch.setattr(smoke.subprocess, "Popen", start)
    yield start, processes, child_pids
    # Also clean the historical implementation's leaks after a failing oracle.
    for pid in child_pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for process in processes:
        if process.poll() is None:
            process.kill()
        type(process).communicate(process, timeout=10)


def assert_stopped(processes, child_pids):
    assert all(process.poll() is not None for process in processes)
    for pid in child_pids:
        # An orphan zombie cannot write into HOME and may await the OS reaper.
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        )
        assert not result.stdout.strip() or result.stdout.strip().startswith("Z")


@pytest.mark.parametrize(
    "check",
    [
        "assert_install_waits_for_promotion_guard",
        "assert_legacy_lock_timeout_preserves_live_owner",
        "assert_concurrent_release_ids_are_distinct",
    ],
)
def test_timeout_stops_every_installer_before_propagating(
    check, tmp_path, monkeypatch, smoke, installers
):
    start, processes, child_pids = installers
    expected = subprocess.TimeoutExpired("synthetic installer", 120)

    def start_with_timeout(*args, **kwargs):
        process = start(*args, **kwargs)
        communicate = process.communicate

        def fail_once(*args, **kwargs):
            process.communicate = communicate
            raise expected

        process.communicate = fail_once
        return process

    monkeypatch.setattr(smoke.subprocess, "Popen", start_with_timeout)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        getattr(smoke, check)(tmp_path / "install")
    assert caught.value is expected
    # Use the unpatched Popen for the independent process-liveness oracle.
    monkeypatch.undo()
    assert_stopped(processes, child_pids)


def test_second_spawn_failure_stops_first_installer(
    tmp_path, monkeypatch, smoke, installers
):
    start, processes, child_pids = installers
    expected = OSError("synthetic spawn failure")

    def fail_second(*args, **kwargs):
        if processes:
            raise expected
        return start(*args, **kwargs)

    monkeypatch.setattr(smoke.subprocess, "Popen", fail_second)
    with pytest.raises(OSError) as caught:
        smoke.assert_concurrent_release_ids_are_distinct(tmp_path / "install")
    assert caught.value is expected
    monkeypatch.undo()
    assert_stopped(processes, child_pids)


def test_empty_lock_probe_stops_term_resistant_descendant(
    tmp_path, monkeypatch, smoke, installers
):
    _, processes, child_pids = installers
    smoke.assert_empty_legacy_lock_is_reaped(tmp_path / "install")
    monkeypatch.undo()
    assert_stopped(processes, child_pids)


@pytest.mark.parametrize("failure", [AssertionError, KeyboardInterrupt])
@pytest.mark.parametrize(
    "check", ["assert_install_waits_for_promotion_guard", "assert_empty_legacy_lock_is_reaped"]
)
def test_probe_failure_stops_tree_and_preserves_exception(
    check, failure, tmp_path, monkeypatch, smoke, installers
):
    _, processes, child_pids = installers
    expected = failure("synthetic probe failure")

    def fail_clock():
        raise expected

    monkeypatch.setattr(smoke, "time", SimpleNamespace(monotonic=fail_clock))
    with pytest.raises(failure) as caught:
        getattr(smoke, check)(tmp_path / "install")
    assert caught.value is expected
    monkeypatch.undo()
    assert_stopped(processes, child_pids)


def test_exited_leader_still_stops_descendants(
    tmp_path, monkeypatch, smoke, installers
):
    _, processes, child_pids = installers
    with smoke.running_installer(smoke.install_env(tmp_path)) as process:
        # Model a leader that exits while a detached-output descendant remains.
        process.terminate()
        process.communicate(timeout=10)
        assert process.returncode is not None
    monkeypatch.undo()
    assert_stopped(processes, child_pids)


def test_pipe_holding_descendant_is_killed_without_masking_failure(
    tmp_path, monkeypatch, smoke, installers
):
    _, processes, child_pids = installers
    expected = AssertionError("synthetic installer failure")
    env = {**smoke.install_env(tmp_path), "TEST_INHERIT_PIPES": "1"}
    with pytest.raises(AssertionError) as caught:
        with smoke.running_installer(env):
            raise expected
    assert caught.value is expected
    monkeypatch.undo()
    assert_stopped(processes, child_pids)
