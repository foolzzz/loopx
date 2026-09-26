"""Auth preflight for providers and agents.

The preflight proves a credential is *reachable* without ever reading or
printing its value:

- ``api_key``: the named env var is set, or the macOS keychain has the entry
  (``security find-generic-password`` without ``-w``/``-g``, which returns the
  item's existence but not its secret);
- ``oauth_cli``: ``claude auth status --json`` reports ``loggedIn`` or
  ``codex login status`` reports a login;
- ``oauth_token``: the named env var is set.

Every probe runs with a timeout and is reduced to a small typed record.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any

from .agents import AgentDefinition
from .providers import Provider

PROVIDER_PREFLIGHT_SCHEMA_VERSION = "loopx_provider_preflight_v0"
AGENT_PREFLIGHT_SCHEMA_VERSION = "loopx_agent_preflight_v0"
DEFAULT_PREFLIGHT_TIMEOUT_SECONDS = 15.0
RUNTIME_BINARIES = {"claude-code": "claude", "codex-cli": "codex"}

# status values
OK = "ok"
MISSING_CREDENTIAL = "missing_credential"
NOT_LOGGED_IN = "not_logged_in"
CLI_MISSING = "cli_missing"
TIMEOUT = "timeout"
PROBE_FAILED = "probe_failed"
UNSUPPORTED = "unsupported"

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _env_present(name: str | None, environ: Mapping[str, str]) -> bool:
    return bool(name) and bool(str(environ.get(name or "", "")).strip())


def _run(
    argv: list[str],
    *,
    environ: Mapping[str, str],
    timeout: float,
    runner: Runner,
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    try:
        return (
            runner(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=dict(environ),
                check=False,
            ),
            None,
        )
    except subprocess.TimeoutExpired:
        return None, TIMEOUT
    except OSError:
        return None, CLI_MISSING


def _which(name: str, environ: Mapping[str, str]) -> str | None:
    return shutil.which(name, path=environ.get("PATH"))


def _keychain_check(
    provider: Provider,
    *,
    environ: Mapping[str, str],
    timeout: float,
    runner: Runner,
    platform: str,
) -> dict[str, Any]:
    keychain = provider.auth.keychain
    assert keychain is not None
    check: dict[str, Any] = {"kind": "keychain", "service": keychain.service}
    if platform != "darwin":
        return {**check, "status": UNSUPPORTED, "detail": "keychain lookups need macOS"}
    security = _which("security", environ) or "/usr/bin/security"
    # Without -w/-g `security` never prints the secret, only item attributes,
    # and its output is discarded here regardless.
    argv = [security, "find-generic-password", "-s", keychain.service]
    if keychain.account:
        argv.extend(["-a", keychain.account])
    completed, error = _run(argv, environ=environ, timeout=timeout, runner=runner)
    if error:
        return {**check, "status": error}
    if completed is not None and completed.returncode == 0:
        return {**check, "status": OK}
    return {**check, "status": MISSING_CREDENTIAL, "detail": "keychain item not found"}


def _claude_login(
    *, environ: Mapping[str, str], timeout: float, runner: Runner
) -> dict[str, Any]:
    check: dict[str, Any] = {"kind": "cli_login", "cli": "claude"}
    binary = _which("claude", environ)
    if binary is None:
        return {**check, "status": CLI_MISSING, "detail": "claude is not on PATH"}
    completed, error = _run(
        [binary, "auth", "status", "--json"],
        environ=environ,
        timeout=timeout,
        runner=runner,
    )
    if error or completed is None:
        return {**check, "status": error or PROBE_FAILED}
    try:
        status = json.loads(completed.stdout or "")
    except json.JSONDecodeError:
        status = None
    if not isinstance(status, Mapping):
        return {
            **check,
            "status": NOT_LOGGED_IN if completed.returncode != 0 else PROBE_FAILED,
            "detail": "claude auth status did not return a JSON object",
        }
    logged_in = status.get("loggedIn") is True
    auth_method = status.get("authMethod")
    return {
        **check,
        "status": OK if logged_in else NOT_LOGGED_IN,
        "auth_method": auth_method if isinstance(auth_method, str) else None,
        **({} if logged_in else {"detail": "run `claude auth login`"}),
    }


def _codex_login(
    *, environ: Mapping[str, str], timeout: float, runner: Runner
) -> dict[str, Any]:
    check: dict[str, Any] = {"kind": "cli_login", "cli": "codex"}
    binary = _which("codex", environ)
    if binary is None:
        return {**check, "status": CLI_MISSING, "detail": "codex is not on PATH"}
    completed, error = _run(
        [binary, "login", "status"], environ=environ, timeout=timeout, runner=runner
    )
    if error or completed is None:
        return {**check, "status": error or PROBE_FAILED}
    text = f"{completed.stdout or ''}\n{completed.stderr or ''}".lower()
    logged_in = (
        completed.returncode == 0
        and "logged in" in text
        and "not logged in" not in text
    )
    method = None
    if logged_in:
        method = "chatgpt" if "chatgpt" in text else "api_key" if "api key" in text else None
    return {
        **check,
        "status": OK if logged_in else NOT_LOGGED_IN,
        "auth_method": method,
        **({} if logged_in else {"detail": "run `codex login`"}),
    }


def check_provider(
    provider: Provider,
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
    runner: Runner = subprocess.run,
    platform: str | None = None,
) -> dict[str, Any]:
    """Return one typed, secret-free preflight record for a provider."""

    environ = dict(os.environ if environ is None else environ)
    platform = platform or sys.platform
    auth = provider.auth
    checks: list[dict[str, Any]] = []
    if auth.type in {"api_key", "oauth_token"}:
        if auth.env:
            checks.append(
                {
                    "kind": "env",
                    "env": auth.env,
                    "status": OK if _env_present(auth.env, environ) else MISSING_CREDENTIAL,
                }
            )
        if auth.type == "api_key" and auth.keychain and not any(
            item["status"] == OK for item in checks
        ):
            checks.append(
                _keychain_check(
                    provider,
                    environ=environ,
                    timeout=timeout,
                    runner=runner,
                    platform=platform,
                )
            )
        ok = any(item["status"] == OK for item in checks)
        status = OK if ok else next(
            (item["status"] for item in reversed(checks) if item["status"] != MISSING_CREDENTIAL),
            MISSING_CREDENTIAL,
        )
    else:
        probe = _claude_login if auth.cli == "claude" else _codex_login
        checks.append(probe(environ=environ, timeout=timeout, runner=runner))
        status = checks[0]["status"]
        ok = status == OK
    return {
        "schema_version": PROVIDER_PREFLIGHT_SCHEMA_VERSION,
        "provider": provider.name,
        "kind": provider.kind,
        "auth_type": auth.type,
        "ok": ok,
        "status": status,
        "checks": checks,
    }


def preflight_agent(
    agent: AgentDefinition,
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
    runner: Runner = subprocess.run,
    platform: str | None = None,
) -> dict[str, Any]:
    """Check an agent can launch: enabled, runtime CLI present, provider auth."""

    environ = dict(os.environ if environ is None else environ)
    binary = RUNTIME_BINARIES[agent.runtime]
    runtime_present = _which(binary, environ) is not None
    provider_record = (
        check_provider(
            agent.provider_config,
            environ=environ,
            timeout=timeout,
            runner=runner,
            platform=platform,
        )
        if agent.provider_config is not None
        else None
    )
    if not agent.enabled:
        status = "disabled"
    elif not runtime_present:
        status = CLI_MISSING
    elif provider_record is None:
        status = "provider_unresolved"
    else:
        status = provider_record["status"]
    return {
        "schema_version": AGENT_PREFLIGHT_SCHEMA_VERSION,
        "agent_id": agent.id,
        "runtime": agent.runtime,
        "runtime_binary": binary,
        "runtime_binary_present": runtime_present,
        "ok": status == OK,
        "status": status,
        "provider": provider_record,
    }
