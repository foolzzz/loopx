"""Dispatcher bookkeeping: the single-instance lock and the private state file.

Everything lives under ``<runtime_root>/dispatch/``. The state file is the
dispatcher's own scheduling memory (running children, cooldowns, opened gates,
crash-retry identities). It is never LoopX goal state: every goal write goes
through ``loopx turn run-once`` or the Todo API.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

DISPATCH_STATE_SCHEMA_VERSION = "loopx_dispatch_state_v0"
DISPATCH_HISTORY_LIMIT = 50


class DispatchLockError(RuntimeError):
    """Another dispatcher already holds the runtime root's lock."""


def dispatch_dir(runtime_root: Path) -> Path:
    return Path(runtime_root).expanduser() / "dispatch"


def dispatch_state_path(runtime_root: Path) -> Path:
    return dispatch_dir(runtime_root) / "state.json"


def dispatch_lock_path(runtime_root: Path) -> Path:
    return dispatch_dir(runtime_root) / "serve.lock"


class DispatchLock:
    """Non-blocking exclusive ``flock`` held for the dispatcher's lifetime."""

    def __init__(self, runtime_root: Path) -> None:
        self.path = dispatch_lock_path(runtime_root)
        self._handle: Any = None

    def acquire(self) -> "DispatchLock":
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise DispatchLockError(
                f"another dispatcher holds {self.path} (pid {read_lock_holder(self.path) or 'unknown'})"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "acquired_at": time.time()}))
        handle.flush()
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None

    def __enter__(self) -> "DispatchLock":
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


def read_lock_holder(path: Path) -> int | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None
    pid = data.get("pid") if isinstance(data, dict) else None
    return pid if isinstance(pid, int) else None


def lock_is_held(runtime_root: Path) -> bool:
    """True when some process holds the dispatcher lock right now."""

    import fcntl

    path = dispatch_lock_path(runtime_root)
    if not path.exists():
        return False
    with open(path, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": DISPATCH_STATE_SCHEMA_VERSION,
        "runs": {},
        "history": [],
        "provider_cooldowns": {},
        "agent_cooldowns": {},
        "todo_cooldowns": {},
        "gates": {},
        "retry_turns": {},
        "orchestrator_baselines": {},
        "agent_slots": {},
        "last_pass": None,
    }


def load_state(runtime_root: Path) -> dict[str, Any]:
    path = dispatch_state_path(runtime_root)
    state = empty_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if isinstance(raw, dict) and raw.get("schema_version") == DISPATCH_STATE_SCHEMA_VERSION:
        for key, default in state.items():
            value = raw.get(key)
            if isinstance(value, type(default)) or (default is None):
                state[key] = value if value is not None else default
    return state


def save_state(runtime_root: Path, state: dict[str, Any]) -> None:
    path = dispatch_state_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["history"] = list(state.get("history") or [])[-DISPATCH_HISTORY_LIMIT:]
    state["updated_at"] = time.time()
    fd, tmp = tempfile.mkstemp(prefix=".state.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
