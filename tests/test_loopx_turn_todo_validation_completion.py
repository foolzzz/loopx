"""Built-in hosts can complete a Todo that declares its own validation command.

The claude-code (and codex) host is offered ``validated_completion`` for a
todo-scoped Turn. The Turn validator (here the Todo's own validation command,
as the dispatcher passes it) gates the result, and the Todo lifecycle runs the
Todo's declared validation again before it completes the Todo, or, under
role_v1 with acceptance required, delivers it to ``in_review``.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

from loopx.cli import main as cli_main
from loopx.control_plane.turn_driver.codex_cli import codex_cli_result_schema
from loopx.control_plane.todos.contract import encode_metadata_value
from tests.test_loopx_turn_driver import _write_live_fixture

GOAL = "loopx-turn-fixture"
TODO = "todo_fixture0001"

FAKE_CLAUDE = """
import json, os, pathlib, re, sys
prompt = sys.stdin.read()
schema = json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
kinds = schema["properties"]["result_kind"]["enum"]
kind = os.environ.get("FAKE_CLAUDE_KIND", "validated_completion")
if kind not in kinds:
    print(json.dumps({"type": "result", "is_error": True, "subtype": "error", "result": "kind not offered"}))
    raise SystemExit(1)
pathlib.Path(os.environ["FIXTURE_ARTIFACT"]).write_text("validated", encoding="utf-8")
turn_key = re.search(r'"turn_key":"([^"]+)"', prompt).group(1)
result = {
    "schema_version": "loopx_turn_result_v0", "turn_key": turn_key, "result_kind": kind,
    "completed_phases": ["host_execute", "typed_result"], "classification": "fixture_done",
    "recommended_action": "Select the next Todo.", "next_action": "Select the next Todo.",
    "delivery_batch_scale": "implementation", "delivery_outcome": "primary_goal_outcome",
    "vision_unchanged_reason": "The fixture objective remains unchanged.",
    "path_delta_mode": "unchanged", "agent_vision_json": "",
    "summary": os.environ.get("FAKE_CLAUDE_SUMMARY", "The fixture Todo is complete."),
    "reward_memory_reflection_json": "",
}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": "s-1", "structured_output": result}))
"""

# The Todo validation runs in the Goal repository and the Turn validator in the
# Turn workspace, so the fixture artifact lives at one absolute path.
VALIDATION = (
    "import os, pathlib, sys\n"
    "sys.stdin.read()\n"
    "artifact = pathlib.Path(os.environ['FIXTURE_ARTIFACT'])\n"
    "raise SystemExit(0 if artifact.is_file() and artifact.read_text() == 'validated' else 7)\n"
)


def _run(argv: list[str]) -> tuple[int, dict]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli_main(argv)
    return code, json.loads(output.getvalue())


def _fixture(tmp_path: Path, *, role_v1: bool) -> tuple[Path, Path, Path, Path, Path]:
    validation = json.dumps([sys.executable, "-c", VALIDATION])
    project, runtime, registry = _write_live_fixture(
        tmp_path, todo_metadata_extra=f"validation_command_argv={encode_metadata_value(validation)}",
    )
    if role_v1:
        payload = json.loads(registry.read_text(encoding="utf-8"))
        coordination = payload["goals"][0]["coordination"]
        coordination["agent_model"] = "role_v1"
        coordination["registered_agents"] = ["codex-fixture", "codex-acceptor", "fable-orch"]
        coordination["agent_roles"] = {"codex-fixture": "developer", "codex-acceptor": "acceptor",
                                       "fable-orch": "orchestrator"}
        registry.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    executable = tmp_path / "bin" / "claude"
    executable.parent.mkdir(parents=True)
    executable.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}", encoding="utf-8")
    executable.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return project, runtime, registry, executable, workspace


def _run_once(project: Path, runtime: Path, registry: Path, executable: Path, workspace: Path,
              agent: str = "codex-fixture"):
    return _run([
        "--registry", str(registry), "--runtime-root", str(runtime), "--format", "json",
        "turn", "run-once", "--host", "claude-code", "--goal-id", GOAL,
        "--agent-id", agent, "--project", str(workspace),
        "--claude-bin", str(executable),
        # The dispatcher passes the Todo's own validation command as the Turn validator.
        "--validation-command-json", json.dumps([sys.executable, "-c", VALIDATION]),
        "--scan-root", str(project), "--no-global-sync", "--execute",
    ])


def _state(project: Path) -> str:
    return (project / ".codex" / "goals" / GOAL / "ACTIVE_GOAL_STATE.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("role_v1", "status"),
    [(False, "done"), (True, "in_review")],
)
def test_claude_code_turn_completes_todo_with_declared_validation(
    tmp_path: Path, role_v1: bool, status: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIXTURE_ARTIFACT", str(tmp_path / "fixture-artifact.txt"))
    project, runtime, registry, executable, workspace = _fixture(tmp_path, role_v1=role_v1)
    assert "validation_command_argv=" in _state(project)
    code, payload = _run_once(project, runtime, registry, executable, workspace)
    assert code == 0, json.dumps(payload)[:3000]
    assert payload["status"] == "committed"
    assert payload["result_kind"] == "validated_completion"
    assert payload["effects"]["state_written"] is True
    assert payload["effects"]["quota_spent"] is True
    assert f"todo_id={TODO} status={status}" in _state(project)


def test_validated_completion_is_offered_only_for_todo_scoped_turns() -> None:
    envelope = {"action": {"selected_todo": {"todo_id": TODO}}}
    kinds = codex_cli_result_schema({"turn_envelope": envelope})["properties"]["result_kind"]["enum"]
    assert "validated_completion" in kinds
    kinds = codex_cli_result_schema({"turn_envelope": {"action": {}}})["properties"]["result_kind"]["enum"]
    assert "validated_completion" not in kinds


def test_acceptor_turns_reject_then_accept_a_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fork S2 end to end through run-once: deliver, reject, redeliver, accept."""

    monkeypatch.setenv("FIXTURE_ARTIFACT", str(tmp_path / "fixture-artifact.txt"))
    project, runtime, registry, executable, workspace = _fixture(tmp_path, role_v1=True)
    code, payload = _run_once(project, runtime, registry, executable, workspace)
    assert code == 0, json.dumps(payload)[:3000]
    assert f"todo_id={TODO} status=in_review" in _state(project)

    # The acceptor's repair_required result is its reject verdict.
    monkeypatch.setenv("FAKE_CLAUDE_KIND", "repair_required")
    monkeypatch.setenv("FAKE_CLAUDE_SUMMARY", "Pagination is missing from the endpoint.")
    code, payload = _run_once(project, runtime, registry, executable, workspace, "codex-acceptor")
    assert code == 0, json.dumps(payload)[:3000]
    state = _state(project)
    assert f"todo_id={TODO} status=open" in state
    assert "reject_count=1" in state
    assert "Pagination%20is%20missing" in state

    monkeypatch.setenv("FAKE_CLAUDE_KIND", "validated_completion")
    monkeypatch.setenv("FAKE_CLAUDE_SUMMARY", "Pagination added.")
    code, payload = _run_once(project, runtime, registry, executable, workspace)
    assert code == 0, json.dumps(payload)[:3000]
    assert f"todo_id={TODO} status=in_review" in _state(project)
    code, payload = _run_once(project, runtime, registry, executable, workspace, "codex-acceptor")
    assert code == 0, json.dumps(payload)[:3000]
    assert payload["result_kind"] == "validated_completion"
    assert f"todo_id={TODO} status=done" in _state(project)
