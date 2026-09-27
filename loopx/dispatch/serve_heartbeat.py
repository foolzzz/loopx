"""A low-rate idle heartbeat for ``loopx dispatch serve`` (fork pilot v1 gap N11).

``serve`` prints a pass only when it acts (launches, reaps, opens a gate or
errors), so a goal with nothing to do looked the same as a dead dispatcher.
When nothing was printed for ``interval`` seconds, one compact line says the
dispatcher is alive and why it is idle: the pass count and the skip reasons
since the last line. It is a log line only; no state is written.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

DISPATCH_IDLE_HEARTBEAT_SCHEMA_VERSION = "loopx_dispatch_idle_heartbeat_v0"
DEFAULT_IDLE_HEARTBEAT_SECONDS = 900.0
MAX_IDLE_HEARTBEAT_REASONS = 8


def pass_is_loggable(report: Mapping[str, Any]) -> bool:
    return bool(report.get("launched") or report.get("reaped") or report.get("errors") or report.get("gates_opened"))


class IdleHeartbeat:
    """Emit one idle line when no pass was logged for ``interval`` seconds (0 disables)."""

    def __init__(self, interval: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval = max(0.0, float(interval))
        self._clock = clock
        self._last_line = clock()
        self._passes = 0
        self._reasons: Counter[str] = Counter()

    def observe(self, report: Mapping[str, Any], *, running_turns: int = 0) -> dict[str, Any] | None:
        """Record one pass; return the heartbeat to print, if one is due."""

        now = self._clock()
        if pass_is_loggable(report):
            self._reset(now)
            return None
        self._passes += 1
        for item in report.get("skipped") or []:
            if isinstance(item, Mapping) and item.get("reason"):
                self._reasons[str(item["reason"])] += 1
        if not self.interval or now - self._last_line < self.interval:
            return None
        heartbeat = {
            "schema_version": DISPATCH_IDLE_HEARTBEAT_SCHEMA_VERSION,
            "idle": True,
            "idle_seconds": round(now - self._last_line, 1),
            "passes": self._passes,
            "skip_reasons": dict(self._reasons.most_common(MAX_IDLE_HEARTBEAT_REASONS)),
            "running_turns": int(running_turns),
        }
        self._reset(now)
        return heartbeat

    def _reset(self, now: float) -> None:
        self._last_line = now
        self._passes = 0
        self._reasons.clear()
