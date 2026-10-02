"""Default promotion must serialize source-bundle preparation as well as copying."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX installer guard")


@pytest.mark.parametrize("promotion", ["1", "0", "auto"])
def test_promotion_guard_precedes_chat_bundle_preparation(tmp_path, promotion):
    import fcntl

    source = tmp_path / "source"
    scripts = source / "scripts"
    scripts.mkdir(parents=True)
    installer = scripts / "install-local.sh"
    shutil.copy2(Path(__file__).resolve().parents[1] / "scripts/install-local.sh", installer)
    (scripts / "chat_bundle.py").write_text(
        "import os, pathlib\n"
        "pathlib.Path(os.environ['TEST_CHAT_STARTED']).touch()\n"
        "raise SystemExit(7)\n"
    )
    guard_started = tmp_path / "guard-started"
    chat_started = tmp_path / "chat-started"
    wrapper = tmp_path / "python-wrapper"
    wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "-" ] && [ -z "${LOOPX_INSTALL_GUARD_FILE:-}" ]; then\n'
        '  cat >/dev/null\n'
        '  printf "%s\\n" "$0"\n'
        '  exit 0\n'
        'fi\n'
        'if [ -z "${LOOPX_INSTALL_GUARD_FILE:-}" ] || [ "${LOOPX_INSTALL_GUARD_HELD:-0}" = "1" ]; then\n'
        '  exec "$TEST_REAL_PYTHON" "$@"\n'
        'fi\n'
        'touch "$TEST_GUARD_STARTED"\n'
        'exec "$TEST_REAL_PYTHON" "$@"\n'
    )
    wrapper.chmod(0o755)
    releases = tmp_path / "home/releases"
    releases.mkdir(parents=True)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "home/.codex"),
        "LOOPX_PYTHON": str(wrapper),
        "LOOPX_RELEASES_DIR": str(releases),
        "LOOPX_PROMOTE_DEFAULT": promotion,
        "TEST_REAL_PYTHON": sys.executable,
        "TEST_GUARD_STARTED": str(guard_started),
        "TEST_CHAT_STARTED": str(chat_started),
    }
    env.pop("LOOPX_INSTALL_GUARD_HELD", None)
    env.pop("LOOPX_INSTALL_GUARD_FILE", None)
    with (releases / ".install-guard").open("a+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        process = subprocess.Popen(
            [str(installer)], env=env, cwd=source, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not guard_started.exists() and not chat_started.exists() and process.poll() is None:
                assert time.monotonic() < deadline, "installer did not reach its preflight"
                time.sleep(0.01)
            if promotion == "1":
                assert guard_started.exists(), "Chat preparation ran before attempting the promotion guard"
                assert not chat_started.exists(), "Chat preparation bypassed the held promotion guard"
                assert process.poll() is None
            else:
                assert chat_started.exists(), "Canary-only preparation was blocked by the promotion guard"
                assert not guard_started.exists()
            fcntl.flock(guard, fcntl.LOCK_UN)
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 7, (stdout, stderr)
            assert chat_started.exists()
        finally:
            fcntl.flock(guard, fcntl.LOCK_UN)
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate(timeout=10)
