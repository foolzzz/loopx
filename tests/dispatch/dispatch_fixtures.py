"""Hermetic fixtures for dispatcher tests: temp registry, agents, fake CLIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

GOAL_ID = "dispatch-fixture"

PROVIDERS_YAML = """
providers:
  anthropic-login:
    kind: anthropic
    auth: {type: oauth_cli, cli: claude}
  codex-login:
    kind: openai
    auth: {type: oauth_cli, cli: codex}
"""

# One fake `claude`: `auth status --json` for the preflight, `-p` for a Turn.
FAKE_CLAUDE = r'''
import json, os, pathlib, re, sys
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    logged_in = os.environ.get("FAKE_CLAUDE_AUTH", "in") == "in"
    print(json.dumps({"loggedIn": logged_in, "authMethod": "claude.ai" if logged_in else "none"}))
    raise SystemExit(0 if logged_in else 1)
prompt = sys.stdin.read()
system_prompt = ""
if "--append-system-prompt-file" in args:
    system_prompt = pathlib.Path(args[args.index("--append-system-prompt-file") + 1]).read_text(encoding="utf-8")
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": args, "cwd": os.getcwd(), "prompt": prompt,
                                 "system_prompt": system_prompt}) + "\n")
match = re.search(r'"turn_key":"([^"]+)"', prompt)
turn_key = match.group(1) if match else ""
barrier = os.environ.get("FAKE_CLAUDE_BARRIER_DIR")
if barrier:
    # Overlap probe: wait until N host processes are running at once, so a
    # test can prove that Turns really overlapped instead of running serially.
    import time
    started = time.time()
    pathlib.Path(barrier).mkdir(parents=True, exist_ok=True)
    pathlib.Path(barrier, f"{os.getpid()}.start").write_text(str(started), encoding="utf-8")
    wanted = int(os.environ.get("FAKE_CLAUDE_BARRIER_COUNT", "2"))
    deadline = started + float(os.environ.get("FAKE_CLAUDE_BARRIER_SECONDS", "30"))
    while len(list(pathlib.Path(barrier).glob("*.start"))) < wanted and time.time() < deadline:
        time.sleep(0.05)
    met = len(list(pathlib.Path(barrier).glob("*.start"))) >= wanted
    pathlib.Path(barrier, f"{os.getpid()}.end").write_text(
        json.dumps({"started": started, "ended": time.time(), "met": met, "cwd": os.getcwd()}), encoding="utf-8")
pathlib.Path("fixture-artifact.txt").write_text("validated", encoding="utf-8")
if os.environ.get("FAKE_CLAUDE_COMMIT") == "1":
    import subprocess
    subprocess.run(["git", "add", "fixture-artifact.txt"], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "fixture artifact"], check=True, capture_output=True)
result = {
    "schema_version": "loopx_turn_result_v0",
    "turn_key": turn_key,
    "result_kind": os.environ.get("FAKE_CLAUDE_RESULT_KIND", "validated_progress"),
    "completed_phases": ["host_execute", "typed_result"],
    "classification": "fixture_progress",
    "recommended_action": "Continue the public fixture",
    "next_action": "Run the next public fixture check",
    "delivery_batch_scale": "implementation",
    "delivery_outcome": "outcome_progress",
    "vision_unchanged_reason": "The fixture objective remains unchanged.",
    "path_delta_mode": "unchanged",
    "agent_vision_json": "",
    "summary": "One public fixture advanced in fixture-artifact.txt.",
    "reward_memory_reflection_json": "",
}
if os.environ.get("FAKE_CLAUDE_RESULT_KIND"):
    # G12 acceptor verdicts: accept, reject, blocked (user_action_required).
    result["result_kind"] = os.environ["FAKE_CLAUDE_RESULT_KIND"]
    result["summary"] = os.environ.get("FAKE_CLAUDE_SUMMARY", "")
    if result["result_kind"] in {"user_action_required", "wait"}:
        result["path_delta_mode"] = ""
        result["vision_unchanged_reason"] = ""
if os.environ.get("FAKE_CLAUDE_MODE") == "invalid_vision":
    # The E2E pilot's orchestrator result: a vision packet the contract rejects.
    result.update(vision_unchanged_reason="", path_delta_mode="material_replan",
                  agent_vision_json=json.dumps({"vision_patch": {"advancement_policy": "sometimes"}}))
if os.environ.get("FAKE_CLAUDE_MODE") == "rate_limited":
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "api_error_status": 429, "result": "API Error: 429"}))
    raise SystemExit(1)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "api_error_status": None, "session_id": "s-1",
                  "result": json.dumps(result), "structured_output": result}))
'''

FAKE_CODEX = r'''
import os, sys
if sys.argv[1:3] == ["login", "status"]:
    if os.environ.get("FAKE_CODEX_AUTH", "in") == "in":
        print("Logged in using ChatGPT"); raise SystemExit(0)
    print("Not logged in"); raise SystemExit(1)
raise SystemExit(2)
'''

# Stands in for `python -m loopx.cli ... turn run-once`. Behaviour per agent
# comes from a JSON modes file; each launch is appended to a JSONL log.
FAKE_LOOPX = r'''
import json, os, pathlib, sys, time
argv = sys.argv[1:]
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
agent = opt("--agent-id")
modes = json.loads(pathlib.Path(os.environ["FAKE_TURN_MODES"]).read_text() or "{}")
queue = modes.get(agent) or ["ok"]
counter_path = pathlib.Path(os.environ["FAKE_TURN_MODES"] + "." + agent + ".count")
count = int(counter_path.read_text()) if counter_path.exists() else 0
counter_path.write_text(str(count + 1))
mode = queue[min(count, len(queue) - 1)]
prompt_file = opt("--claude-system-prompt-file")
with open(os.environ["FAKE_TURN_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "argv": argv, "cwd": os.getcwd(), "agent": agent, "mode": mode, "pid": os.getpid(),
        "system_prompt": pathlib.Path(prompt_file).read_text() if prompt_file else None,
    }) + "\n")
if mode == "hold":
    release = pathlib.Path(os.environ["FAKE_TURN_RELEASE"])
    deadline = time.time() + 60
    while not release.exists() and time.time() < deadline:
        time.sleep(0.05)
    mode = "ok"
if mode == "crash":
    print("Traceback: simulated crash", file=sys.stderr)
    raise SystemExit(9)
if mode == "ok":
    print(json.dumps({"ok": True, "status": "committed", "effects": {"host_invoked": True}}))
    raise SystemExit(0)
if mode == "failed":
    # A Turn whose independent validation failed: no host failure kind.
    print(json.dumps({"ok": False, "status": "failed", "reason": "validation_failed",
                      "effects": {"host_invoked": True}}))
    raise SystemExit(1)
kind = {"rate_limited": "rate_limited", "quota": "quota_exhausted", "auth": "auth_failed"}[mode]
print(json.dumps({"ok": False, "status": "failed", "host_failure": {"kind": kind, "retryable": True}}))
raise SystemExit(1)
'''


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def git_env(tmp_path: Path) -> dict[str, str]:
    config = tmp_path / "gitconfig"
    config.write_text("", encoding="utf-8")
    return {
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


def make_repo(root: Path, name: str) -> Path:
    repo = root / "repos" / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text(f"{name}\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    return repo


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def write_fixture(
    tmp_path: Path,
    *,
    agents: dict[str, dict[str, Any]],
    todo_lines: list[str] | None = None,
    repos: dict[str, Path] | None = None,
    agent_model: str = "role_v1",
) -> dict[str, Any]:
    """Create a role_v1 (or ``agent_model``) goal, agent files, providers and fake CLIs.

    ``agents`` maps agent id to ``{role, runtime?, provider?, max_concurrency?,
    system_prompt?}``.
    """

    project = tmp_path / "project"
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    state = project / ".codex" / "goals" / GOAL_ID / "ACTIVE_GOAL_STATE.md"
    state.parent.mkdir(parents=True)
    state.write_text(
        "\n".join(
            [
                "---",
                "status: active",
                "updated_at: 2026-01-01T00:00:00+00:00",
                "---",
                "",
                "# Dispatch Fixture",
                "",
                "## Agent Todo",
                "",
                *(todo_lines or []),
                "",
            ]
        ),
        encoding="utf-8",
    )
    goal: dict[str, Any] = {
        "id": GOAL_ID,
        "domain": "dispatch-public-fixture",
        "status": "active",
        "repo": str(project),
        "state_file": str(state.relative_to(project)),
        "adapter": {"kind": "fixture_v0", "status": "connected-delivery"},
        "quota": {"compute": 10.0, "window_hours": 24},
        "coordination": {
            "agent_model": agent_model,
            "registered_agents": sorted(agents),
            "agent_roles": {agent_id: spec["role"] for agent_id, spec in agents.items()},
            "write_scope": ["docs/**"],
        },
    }
    if repos:
        goal["repos"] = [
            {"name": name, "path": str(path), "default_branch": "main", "merge_target": "main"}
            for name, path in repos.items()
        ]
    registry = project / ".loopx" / "registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps({"schema_version": 1, "common_runtime_root": str(runtime), "goals": [goal]}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (runtime / "providers.yaml").write_text(PROVIDERS_YAML, encoding="utf-8")
    agents_dir = runtime / "agents"
    agents_dir.mkdir()
    for agent_id, spec in agents.items():
        runtime_name = spec.get("runtime", "claude-code")
        provider = spec.get("provider") or (
            "anthropic-login" if runtime_name == "claude-code" else "codex-login"
        )
        lines = [
            f"role: {spec['role']}",
            f"runtime: {runtime_name}",
            f"provider: {provider}",
            f"max_concurrency: {spec.get('max_concurrency', 1)}",
        ]
        if spec.get("enabled") is False:
            lines.append("enabled: false")
        if spec.get("system_prompt"):
            prompt = agents_dir / f"{agent_id}.md"
            prompt.write_text(spec["system_prompt"], encoding="utf-8")
            lines.append(f"system_prompt_file: {prompt.name}")
        if runtime_name == "claude-code":
            lines.append("permission_mode: acceptEdits")
        (agents_dir / f"{agent_id}.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    _script(bin_dir / "claude", FAKE_CLAUDE)
    _script(bin_dir / "codex", FAKE_CODEX)
    fake_loopx = _script(tmp_path / "fake_loopx.py", FAKE_LOOPX)
    modes = tmp_path / "turn-modes.json"
    modes.write_text("{}", encoding="utf-8")
    environ = {
        **{key: value for key, value in os.environ.items() if key in {"HOME", "LANG", "TMPDIR", "PYTHONPATH"}},
        **git_env(tmp_path),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_TURN_LOG": str(tmp_path / "turns.jsonl"),
        "FAKE_TURN_MODES": str(modes),
        "FAKE_TURN_RELEASE": str(tmp_path / "release"),
        "FAKE_CLAUDE_LOG": str(tmp_path / "claude.jsonl"),
    }
    return {
        "project": project,
        "runtime": runtime,
        "registry": registry,
        "state": state,
        "bin": bin_dir,
        "fake_loopx": fake_loopx,
        "modes": modes,
        "environ": environ,
        "turn_log": tmp_path / "turns.jsonl",
        "claude_log": tmp_path / "claude.jsonl",
        "release": tmp_path / "release",
    }


def set_modes(fixture: dict[str, Any], modes: dict[str, list[str]]) -> None:
    fixture["modes"].write_text(json.dumps(modes), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
