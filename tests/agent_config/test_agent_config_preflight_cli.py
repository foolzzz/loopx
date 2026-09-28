from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

from loopx.agent_config import check_provider, load_providers, preflight_agent, resolve_agent
from loopx.cli import main as cli_main

SECRET = "sk-fixture-secret-value-0001"

PROVIDERS = """
providers:
  claude-login:
    kind: anthropic
    auth: {type: oauth_cli}
  codex-login:
    kind: openai
    auth: {type: oauth_cli}
  token:
    kind: anthropic
    auth: {type: oauth_token, env: FIXTURE_OAUTH_TOKEN}
  keyed:
    kind: anthropic
    auth: {type: api_key, env: FIXTURE_API_KEY, keychain: {service: fixture-svc}}
"""


def _script(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture()
def fake_bin(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    _script(
        directory,
        "claude",
        """
import json, os, sys, time
mode = os.environ.get("FAKE_CLAUDE_AUTH", "in")
if mode == "hang":
    time.sleep(30)
if sys.argv[1:4] != ["auth", "status", "--json"]:
    raise SystemExit(2)
print(json.dumps({"loggedIn": mode == "in", "authMethod": "oauth_token" if mode == "in" else "none"}))
raise SystemExit(0 if mode == "in" else 1)
""",
    )
    _script(
        directory,
        "codex",
        """
import os, sys
if os.environ.get("FAKE_CODEX_AUTH", "in") == "in":
    print("Logged in using ChatGPT")
    raise SystemExit(0)
print("Not logged in", file=sys.stderr)
raise SystemExit(1)
""",
    )
    _script(
        directory,
        "security",
        f"""
import os, sys
# A preflight must never ask for the secret itself.
if "-w" in sys.argv or "-g" in sys.argv:
    print({SECRET!r})
    raise SystemExit(3)
raise SystemExit(0 if os.environ.get("FAKE_KEYCHAIN") == "present" else 44)
""",
    )
    return directory


def _env(fake_bin: Path, **extra: str) -> dict[str, str]:
    return {"PATH": str(fake_bin), **extra}


@pytest.fixture()
def runtime(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "providers.yaml").write_text(PROVIDERS, encoding="utf-8")
    return root


def test_oauth_cli_claude_logged_in_and_out(runtime: Path, fake_bin: Path) -> None:
    provider = load_providers(runtime)["claude-login"]
    ok = check_provider(provider, environ=_env(fake_bin))
    assert ok["ok"] is True
    assert ok["checks"][0]["auth_method"] == "oauth_token"

    out = check_provider(provider, environ=_env(fake_bin, FAKE_CLAUDE_AUTH="out"))
    assert out["ok"] is False
    assert out["status"] == "not_logged_in"


def test_oauth_cli_timeout_and_missing_cli(runtime: Path, fake_bin: Path, tmp_path: Path) -> None:
    provider = load_providers(runtime)["claude-login"]
    hung = check_provider(
        provider, environ=_env(fake_bin, FAKE_CLAUDE_AUTH="hang"), timeout=0.5
    )
    assert hung["status"] == "timeout"
    empty = tmp_path / "empty"
    empty.mkdir()
    missing = check_provider(provider, environ={"PATH": str(empty)})
    assert missing["status"] == "cli_missing"


def test_oauth_cli_codex(runtime: Path, fake_bin: Path) -> None:
    provider = load_providers(runtime)["codex-login"]
    assert check_provider(provider, environ=_env(fake_bin))["checks"][0]["auth_method"] == "chatgpt"
    out = check_provider(provider, environ=_env(fake_bin, FAKE_CODEX_AUTH="out"))
    assert out["status"] == "not_logged_in"


def test_oauth_token_env(runtime: Path, fake_bin: Path) -> None:
    provider = load_providers(runtime)["token"]
    present = check_provider(provider, environ=_env(fake_bin, FIXTURE_OAUTH_TOKEN=SECRET))
    assert present["ok"] is True
    assert SECRET not in json.dumps(present)
    assert check_provider(provider, environ=_env(fake_bin))["status"] == "missing_credential"


def test_api_key_env_then_keychain(runtime: Path, fake_bin: Path) -> None:
    provider = load_providers(runtime)["keyed"]
    via_env = check_provider(provider, environ=_env(fake_bin, FIXTURE_API_KEY=SECRET), platform="darwin")
    assert via_env["ok"] is True
    assert [item["kind"] for item in via_env["checks"]] == ["env"]
    assert SECRET not in json.dumps(via_env)

    via_keychain = check_provider(
        provider, environ=_env(fake_bin, FAKE_KEYCHAIN="present"), platform="darwin"
    )
    assert via_keychain["ok"] is True
    assert [item["status"] for item in via_keychain["checks"]] == ["missing_credential", "ok"]
    assert SECRET not in json.dumps(via_keychain)

    neither = check_provider(provider, environ=_env(fake_bin), platform="darwin")
    assert neither["ok"] is False
    assert neither["status"] == "missing_credential"

    linux = check_provider(provider, environ=_env(fake_bin), platform="linux")
    assert linux["checks"][-1]["status"] == "unsupported"


def test_agent_preflight_reports_runtime_binary(runtime: Path, fake_bin: Path, tmp_path: Path) -> None:
    agents = runtime / "agents"
    agents.mkdir()
    (agents / "dev.yaml").write_text(
        "role: developer\nruntime: claude-code\nprovider: claude-login\n", encoding="utf-8"
    )
    agent = resolve_agent("dev", runtime_root=runtime)
    assert preflight_agent(agent, environ=_env(fake_bin))["ok"] is True
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert preflight_agent(agent, environ={"PATH": str(empty)})["status"] == "cli_missing"


def _cli(runtime: Path, *argv: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli_main(["--runtime-root", str(runtime), "--format", "json", *argv])
    return code, json.loads(output.getvalue())


def test_cli_agent_and_provider_commands(
    runtime: Path, fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setenv("FIXTURE_OAUTH_TOKEN", SECRET)
    monkeypatch.delenv("FIXTURE_API_KEY", raising=False)
    monkeypatch.setenv("FAKE_KEYCHAIN", "present")
    monkeypatch.chdir(tmp_path)
    agents = runtime / "agents"
    agents.mkdir()
    (agents / "dev.yaml").write_text(
        "role: developer\nruntime: claude-code\nprovider: claude-login\nmodel: opus\n",
        encoding="utf-8",
    )
    project = tmp_path / "proj"
    (project / ".loopx" / "agents").mkdir(parents=True)
    (project / ".loopx" / "agents" / "dev.yaml").write_text("model: haiku\n", encoding="utf-8")
    (project / ".loopx" / "agents" / "broken.yaml").write_text(
        "role: developer\nruntime: claude-code\nprovider: ghost\n", encoding="utf-8"
    )

    code, listed = _cli(runtime, "agent", "list")
    assert code == 0
    assert [row["id"] for row in listed["agents"]] == ["dev"]

    code, shown = _cli(runtime, "agent", "show", "dev", "--project", str(project))
    assert code == 0
    assert shown["agent"]["model"] == "haiku"
    assert shown["turn_run_once_host_args"][:2] == ["--host", "claude-code"]

    code, validated = _cli(runtime, "agent", "validate", "--project", str(project))
    assert code == 1
    assert validated["invalid_count"] == 1
    broken = next(row for row in validated["agents"] if row["id"] == "broken")
    assert "not defined in providers.yaml" in broken["issues"][0]

    code, one = _cli(runtime, "agent", "validate", "dev", "--project", str(project), "--check-auth")
    assert code == 0
    assert one["agents"][0]["preflight"]["ok"] is True

    code, providers = _cli(runtime, "provider", "list")
    assert code == 0
    assert providers["provider_count"] == 4

    code, check = _cli(runtime, "provider", "check", "--name", "token")
    assert code == 0 and check["checks"][0]["status"] == "ok"

    code, all_checks = _cli(runtime, "provider", "check")
    statuses = {item["provider"]: item["status"] for item in all_checks["checks"]}
    assert statuses["claude-login"] == "ok"
    assert statuses["codex-login"] == "ok"
    if sys.platform == "darwin":
        assert statuses["keyed"] == "ok"
    assert SECRET not in json.dumps(all_checks)

    code, missing = _cli(runtime, "provider", "check", "--name", "ghost")
    assert code == 1 and missing["error_code"] == "provider_not_found"

    markdown = io.StringIO()
    with contextlib.redirect_stdout(markdown):
        cli_main(["--runtime-root", str(runtime), "agent", "validate", "--project", str(project)])
    assert "broken: INVALID" in markdown.getvalue()


def test_cli_reports_invalid_providers_file(runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (runtime / "providers.yaml").write_text(
        f"providers:\n  - {{name: x, kind: anthropic, auth: {{type: api_key, api_key: {SECRET}}}}}\n",
        encoding="utf-8",
    )
    code, payload = _cli(runtime, "provider", "list")
    assert code == 1
    assert payload["error_code"] == "invalid_config"
    assert SECRET not in json.dumps(payload)
