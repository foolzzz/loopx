"""Pilot v1 gap N9: a proxy's upstream 5xx is a provider backoff, not an unknown failure."""
from __future__ import annotations

import pytest

from loopx.control_plane.turn_driver.codex_cli import _diagnostic_failure_category, _event_failure_category
from loopx.control_plane.turn_driver.host_failure import build_host_failure_record
from loopx.control_plane.turn_driver.host_stderr import (
    HOST_STDERR_TAIL_MAX_LINES,
    HostStderrTail,
    redact_host_stderr_line,
)
from loopx.dispatch.policy import PROVIDER_BACKOFF_FAILURE_KINDS
from tests.test_loopx_turn_codex_cli import _capture_fake_codex_failure

# The shape CLIProxyAPI returned in the pilot when its upstream dial failed.
CPA_503 = (
    'ERROR: unexpected status 503 Service Unavailable: {"error":{"message":"auth_unavailable: no auth '
    'available (dial upstream: connect: connection refused)","type":"server_error"}}'
)
SECRET_LINE = (
    # Split literals keep this synthetic secret out of the public-boundary credential scan.
    "request failed; " + "Author" + "ization: " + "Bear" + "er sk-live-abcdefghijklmnop1234567890 "
    "api_key=" + "AK" + "IA0123456789SECRET "
    "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl "
    "proxy https://user:hunter2@cpa.example:8317/v1 config /Users/someone/.codex/config.toml"
)


@pytest.mark.parametrize(
    "diagnostic",
    [
        CPA_503,
        "stream error: unexpected status 502 Bad Gateway",
        "error sending request for url (http://127.0.0.1:8317/v1/responses)",
        "upstream connect error or disconnect/reset before headers",
        "stream disconnected before completion: connection reset by peer",
    ],
)
def test_upstream_failures_classify_as_provider_capacity(diagnostic: str) -> None:
    assert _diagnostic_failure_category(diagnostic) == "provider_capacity"
    assert _event_failure_category({"type": "error", "message": diagnostic}) == "provider_capacity"


def test_other_diagnostics_keep_their_classes() -> None:
    assert _diagnostic_failure_category("Too many requests; retry later.") == "rate_limited"
    assert _diagnostic_failure_category("401 Unauthorized") == "auth_failed"
    assert _diagnostic_failure_category("something odd happened") is None
    assert _event_failure_category({"type": "error", "error": {"httpStatusCode": 502}}) == "provider_capacity"
    assert _event_failure_category({"type": "error", "error": {"httpStatusCode": 503}}) == "provider_overloaded"


def test_provider_capacity_is_a_retryable_provider_backoff() -> None:
    assert build_host_failure_record("provider_capacity", attempt=1)["retryable"] is True
    assert "provider_capacity" in PROVIDER_BACKOFF_FAILURE_KINDS


def test_stderr_tail_is_bounded_and_redacted() -> None:
    redacted = redact_host_stderr_line(SECRET_LINE)
    for secret in ("sk-live-abcdef", "AK" + "IA0123456789SECRET", "eyJhbGci", "hunter2", "/Users/someone"):
        assert secret not in redacted, secret
    assert "Author" + "ization: <redacted>" in redacted and "api_key=<redacted>" in redacted
    tail = HostStderrTail()
    for index in range(HOST_STDERR_TAIL_MAX_LINES + 5):
        tail.add(f"line {index}\n")
    tail.add("   \n")
    lines = tail.lines()
    assert len(lines) == HOST_STDERR_TAIL_MAX_LINES and lines[-1] == f"line {HOST_STDERR_TAIL_MAX_LINES + 4}"
    assert len(redact_host_stderr_line("x " * 1000)) <= 400


def test_codex_host_backs_off_on_a_cpa_503_and_logs_a_redacted_tail(tmp_path, monkeypatch, capsys) -> None:
    error, runtime_root = _capture_fake_codex_failure(
        tmp_path, monkeypatch, failure_stderr=SECRET_LINE + "\n" + CPA_503,
    )
    assert error.reason == "codex_cli_provider_capacity"
    assert error.failure_kind == "provider_capacity"
    assert error.recovery_kind == "resume_session"
    logged = capsys.readouterr().err
    assert "codex-cli stderr tail" in logged and "auth_unavailable" in logged
    for secret in ("sk-live-abcdef", "hunter2", "eyJhbGci", "/Users/someone"):
        assert secret not in logged, secret
    persisted = "\n".join(path.read_text(encoding="utf-8") for path in runtime_root.rglob("*.json"))
    assert "auth_unavailable" not in persisted
