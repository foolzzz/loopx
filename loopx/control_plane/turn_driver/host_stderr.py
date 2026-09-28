"""Bounded, redacted host stderr tails and upstream failure hints (fork pilot v1 gap N9).

The codex-cli host read stderr only to match a few provider phrases and then
threw it away, so a CLIProxyAPI (CPA) ``503 auth_unavailable ... dial
upstream`` became ``unknown``: non-retryable, a todo backoff instead of a
provider backoff. This module keeps the last few stderr lines, redacted, for
the Turn's own stderr (the dispatcher's ``runs/<id>.err.log``), and names the
upstream failures a proxy reports as ``provider_capacity``.

Redaction runs before a line is kept: bearer and basic credentials, API-key
and token assignments, ``sk-``/JWT-shaped and other long opaque strings, URL
user info and absolute local paths never reach the tail.
"""

from __future__ import annotations

import re
from collections import deque

HOST_STDERR_TAIL_MAX_LINES = 20
HOST_STDERR_TAIL_MAX_LINE_CHARS = 400
HOST_STDERR_REDACTED = "<redacted>"

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*\S+(?:\s+\S+)?"), r"\1: <redacted>"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <redacted>"),
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_-]*(?:api[_-]?key|token|secret|password|passwd|credential|cookie|session[_-]?id)"
            r"[A-Za-z0-9_-]*)(\"?\s*[:=]\s*\"?)[^\s\"',;&]+"
        ),
        r"\1\2<redacted>",
    ),
    (re.compile(r"\b(?:sk|pk|rk|ak)-[A-Za-z0-9_-]{8,}"), HOST_STDERR_REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]+)?"), HOST_STDERR_REDACTED),
    (re.compile(r"(?i)\b(https?|wss?)://[^/\s:@]+:[^/\s@]+@"), r"\1://<redacted>@"),
    (re.compile(r"/(?:Users|home|private|tmp|var|root)/[^\s`\"'<>|,)]+"), "<path>"),
    # Any remaining long opaque run (keys, digests of secrets, session ids).
    (re.compile(r"\b[A-Za-z0-9_+/=-]{32,}\b"), HOST_STDERR_REDACTED),
)

# What a proxy or the network says when the upstream provider is unavailable.
_UPSTREAM_STATUS = re.compile(
    r"(?i)(?:status|http|code|error)[^0-9\n]{0,24}\b5(?:00|02|03|04|20|21|22|23|24|29)\b"
    r"|\b5(?:00|02|03|04)\s+(?:internal server error|bad gateway|service unavailable|gateway time-?out)"
)
_UPSTREAM_MARKERS = (
    "auth_unavailable",
    "dial upstream",
    "upstream connect error",
    "upstream request timeout",
    "no healthy upstream",
    "connection refused",
    "connection reset",
    "error sending request",
    "stream disconnected before completion",
    "dial tcp",
    "no such host",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)


def redact_host_stderr_line(line: str) -> str:
    """One stderr line with credentials and local paths removed, bounded."""

    text = str(line or "").rstrip("\r\n")
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > HOST_STDERR_TAIL_MAX_LINE_CHARS:
        text = text[: HOST_STDERR_TAIL_MAX_LINE_CHARS - 3] + "..."
    return text


def upstream_unavailable(text: str) -> bool:
    """Whether a host diagnostic reports an upstream 5xx or connection failure."""

    lowered = str(text or "").lower()
    return bool(_UPSTREAM_STATUS.search(lowered)) or any(marker in lowered for marker in _UPSTREAM_MARKERS)


class HostStderrTail:
    """The last ``HOST_STDERR_TAIL_MAX_LINES`` redacted, non-blank stderr lines."""

    def __init__(self, max_lines: int = HOST_STDERR_TAIL_MAX_LINES) -> None:
        self._lines: deque[str] = deque(maxlen=max(1, max_lines))

    def add(self, line: str) -> None:
        redacted = redact_host_stderr_line(line)
        if redacted.strip():
            self._lines.append(redacted)

    def lines(self) -> list[str]:
        return list(self._lines)

    def render(self, host: str) -> str:
        if not self._lines:
            return ""
        header = f"{host} stderr tail (last {len(self._lines)} lines, redacted):"
        return "\n".join([header, *(f"  {line}" for line in self._lines)]) + "\n"
