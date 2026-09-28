"""E2E pilot gaps G1 and G3 (fork design decision 29).

G1: a multi-repo Todo's Turn runs from its S5 workspace root
``<runtime_root>/goals/G/workspaces/T/``, which holds one worktree per repo but
is not itself a git worktree. Its delivery must bind to the Todo workspace
identity so the post-settlement refresh passes the peer worktree guard, the
Turn settles, quota is spent and the journal closes.

G3: a repository without ``origin`` delivers under a local ``repo_id``.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

import loopx.cli_commands.turn as turn_command
from loopx.cli import main as cli_main
from loopx.todos import add_goal_todo, list_goal_todos
from loopx.workspace import git_workspace
from tests.dispatch.dispatch_fixtures import git, git_env, make_repo

GOAL = "ws-turn"

FAKE_CLAUDE = """
import json, os, pathlib, re, subprocess, sys
prompt = sys.stdin.read()
kind = os.environ.get("FAKE_CLAUDE_KIND", "validated_completion")
for repo in filter(None, os.environ.get("FAKE_CLAUDE_COMMIT_REPOS", "").split(",")):
    pathlib.Path(repo, "feature.txt").write_text("done\\n", encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "feature"], check=True)
turn_key = re.search(r'"turn_key":"([^"]+)"', prompt).group(1)
result = {
    "schema_version": "loopx_turn_result_v0", "turn_key": turn_key, "result_kind": kind,
    "completed_phases": ["host_execute", "typed_result"], "classification": "fixture_done",
    "recommended_action": "Select the next Todo.", "next_action": "Select the next Todo.",
    "delivery_batch_scale": "implementation", "delivery_outcome": "primary_goal_outcome",
    "vision_unchanged_reason": "The fixture objective remains unchanged.",
    "path_delta_mode": "unchanged", "agent_vision_json": "",
    "summary": "The fixture Todo is complete.", "reward_memory_reflection_json": "",
}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": "s-1", "structured_output": result}))
"""

MULTI_VALIDATION = ["test", "-f", "web/feature.txt"]
SINGLE_VALIDATION = ["test", "-f", "feature.txt"]


def _run(argv: list[str]) -> tuple[int, dict]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli_main(argv)
    return code, json.loads(output.getvalue())


@pytest.fixture()
def pilot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A role_v1 goal (orch/dev/acc) with two local-only repos (no origin)."""

    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    outside = tmp_path / "outside"
    outside.mkdir()
    # The caller's own cwd (this checkout) must never lend the Turn a git identity.
    monkeypatch.chdir(outside)
    home = tmp_path / "progress"
    home.mkdir()
    state = home / "ACTIVE_GOAL_STATE.md"
    state.write_text(
        "---\nstatus: active\nupdated_at: 2026-01-01T00:00:00+00:00\n---\n\n"
        "# Goal\n\n## User Todo\n\n## Agent Todo\n\n## Completed Work Archive\n",
        encoding="utf-8",
    )
    api, web = make_repo(tmp_path, "api"), make_repo(tmp_path, "web")
    runtime = tmp_path / "runtime"
    goal = {
        "id": GOAL, "domain": "loopx-ws-turn-fixture", "status": "active",
        "repo": str(home), "state_file": state.name,
        "quota": {"compute": 1.0, "window_hours": 24},
        "adapter": {"kind": "fixture_v0", "status": "connected-delivery"},
        "repos": [
            {"name": "api", "path": str(api), "default_branch": "main", "merge_target": "task_branch"},
            {"name": "web", "path": str(web), "default_branch": "main", "merge_target": "task_branch"},
        ],
        "coordination": {
            "agent_model": "role_v1", "registered_agents": ["orch", "dev", "acc"],
            "agent_roles": {"orch": "orchestrator", "dev": "developer", "acc": "acceptor"},
        },
    }
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps({"schema_version": 1, "common_runtime_root": str(runtime), "goals": [goal]}),
        encoding="utf-8",
    )
    executable = tmp_path / "bin" / "claude"
    executable.parent.mkdir(parents=True)
    executable.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}", encoding="utf-8")
    executable.chmod(0o755)
    return {"registry": registry, "runtime": runtime, "goal": goal, "claude": executable,
            "api": api, "web": web, "tmp": tmp_path}


def _add_todo(pilot: dict, repos: list[str], validation: list[str], text: str = "") -> tuple[str, Path]:
    added = add_goal_todo(
        registry_path=pilot["registry"], goal_id=GOAL, role="agent",
        text=text or f"Change {' and '.join(repos)}", task_class="advancement_task",
        validation_command_json=json.dumps(validation), claimed_by="dev",
        role_contract={"task_repositories": repos},
    )
    todo_id = str(added["todo_id"])
    prepared = git_workspace.prepare(pilot["goal"], todo_id, repos, pilot["runtime"])
    assert prepared["ok"], prepared
    return todo_id, Path(prepared["workspace_root"])


def _run_once(pilot: dict, agent: str, todo_id: str, workspace: Path, validation: list[str]):
    return _run([
        "--registry", str(pilot["registry"]), "--runtime-root", str(pilot["runtime"]),
        "--format", "json", "turn", "run-once", "--host", "claude-code", "--goal-id", GOAL,
        "--agent-id", agent, "--todo-id", todo_id, "--project", str(workspace),
        "--claude-bin", str(pilot["claude"]),
        "--validation-command-json", json.dumps(validation),
        "--no-global-sync", "--execute",
    ])


def _status(pilot: dict, todo_id: str) -> str:
    rows = list_goal_todos(registry_path=pilot["registry"], goal_id=GOAL,
                           runtime_root_arg=str(pilot["runtime"]))["todos"]
    return next(row["status"] for row in rows if row["todo_id"] == todo_id)


def _runs(pilot: dict) -> list[dict]:
    index = pilot["runtime"] / "goals" / GOAL / "runs" / "index.jsonl"
    if not index.exists():
        return []
    return [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]


def _journals(pilot: dict) -> list[dict]:
    turns = pilot["runtime"] / "goals" / GOAL / "turns"
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(turns.glob("*.json"))]


def _delivery_workspaces(pilot: dict) -> list[dict]:
    return [run["delivery_workspace"] for run in _runs(pilot) if isinstance(run.get("delivery_workspace"), dict)]


def test_multi_repo_turns_settle_from_the_workspace_root(pilot: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    todo_id, root = _add_todo(pilot, ["api", "web"], MULTI_VALIDATION)
    monkeypatch.chdir(root)  # the dispatcher launches the Turn from the workspace root
    monkeypatch.setenv("FAKE_CLAUDE_COMMIT_REPOS", "api,web")
    code, payload = _run_once(pilot, "dev", todo_id, root, MULTI_VALIDATION)
    assert code == 0, json.dumps(payload)[:4000]
    assert payload["status"] == "committed"
    assert payload["effects"]["state_written"] is True
    assert payload["effects"]["quota_spent"] is True
    assert _status(pilot, todo_id) == "in_review"
    assert [journal["status"] for journal in _journals(pilot)] == ["committed"]

    snapshot = _delivery_workspaces(pilot)[-1]
    assert snapshot["identity_kind"] == "todo_workspace"
    assert snapshot["workspace_kind"] == "todo_workspace_root"
    assert snapshot["workspace_identity"] == f"todo-workspace:{GOAL}/{todo_id}"
    identity = snapshot["todo_workspace"]
    assert identity["goal_id"] == GOAL and identity["todo_id"] == todo_id
    assert identity["branch"] == f"loopx/{GOAL}/{todo_id}"
    assert [repo["name"] for repo in identity["repos"]] == ["api", "web"]
    for repo in identity["repos"]:
        assert repo["path"] == repo["name"]  # root-relative, never an absolute path
        assert repo["head_sha"] == git(root / repo["name"], "rev-parse", "HEAD")
        # G3: the pilot repos have no origin.
        assert repo["repo_id"].startswith("local:") and len(repo["repo_id"]) == 70
    assert str(pilot["tmp"]) not in json.dumps(snapshot)

    # The acceptor's verdict Turn runs from the same root and settles too.
    monkeypatch.delenv("FAKE_CLAUDE_COMMIT_REPOS")
    code, payload = _run_once(pilot, "acc", todo_id, root, MULTI_VALIDATION)
    assert code == 0, json.dumps(payload)[:4000]
    assert payload["status"] == "committed"
    assert payload["effects"]["quota_spent"] is True
    assert _status(pilot, todo_id) == "done"
    assert all(journal["status"] == "committed" for journal in _journals(pilot))
    for repo in ("api", "web"):
        assert git(pilot[repo], "show", f"loopx-task/{GOAL}:feature.txt") == "done"


def test_multi_repo_refresh_replays_the_todo_workspace_snapshot(
    pilot: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after the refresh writeback resumes: the refresh replays, then spends."""

    todo_id, root = _add_todo(pilot, ["api", "web"], MULTI_VALIDATION)
    monkeypatch.chdir(root)
    monkeypatch.setenv("FAKE_CLAUDE_COMMIT_REPOS", "api,web")
    real_spend = turn_command.spend_quota_slot
    calls: list[int] = []

    def crash_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("injected crash before quota spend")
        return real_spend(*args, **kwargs)

    monkeypatch.setattr(turn_command, "spend_quota_slot", crash_once)
    code, payload = _run_once(pilot, "dev", todo_id, root, MULTI_VALIDATION)
    assert code == 1 and "injected crash" in payload["error"], payload
    assert payload["effects"]["quota_spent"] is False
    recorded = _delivery_workspaces(pilot)
    assert [item["identity_kind"] for item in recorded] == ["todo_workspace"]

    monkeypatch.delenv("FAKE_CLAUDE_COMMIT_REPOS")
    code, resumed = _run([
        "--registry", str(pilot["registry"]), "--runtime-root", str(pilot["runtime"]),
        "--format", "json", "turn", "run-once", "--host", "claude-code", "--goal-id", GOAL,
        "--agent-id", "dev", "--project", str(root),
        "--claude-bin", str(pilot["claude"]),
        "--validation-command-json", json.dumps(MULTI_VALIDATION), "--no-global-sync",
        "--resume-turn-key", str(payload["resume_turn_key"]), "--execute",
    ])
    assert code == 0, json.dumps(resumed)[:4000]
    assert resumed["status"] == "committed"
    assert resumed["effects"]["host_invoked"] is False
    assert resumed["effects"]["quota_spent"] is True
    assert _status(pilot, todo_id) == "in_review"
    # The replay reused the recorded Todo workspace identity; it did not re-capture.
    assert _delivery_workspaces(pilot)[0] == recorded[0]
    assert all(item["workspace_identity"] == recorded[0]["workspace_identity"]
               for item in _delivery_workspaces(pilot))


def test_a_turn_from_another_todos_workspace_root_is_rejected(
    pilot: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    todo_id, _root = _add_todo(pilot, ["api", "web"], ["true"])
    other_id, other_root = _add_todo(pilot, ["api", "web"], ["true"], text="Another change")
    assert other_id != todo_id
    monkeypatch.chdir(other_root)
    code, payload = _run_once(pilot, "dev", todo_id, other_root, ["true"])
    assert code != 0, json.dumps(payload)[:4000]
    assert payload["effects"]["quota_spent"] is False
    assert "per-Todo workspace root" in json.dumps(payload)
    assert not _delivery_workspaces(pilot)


def test_single_repo_turn_without_origin_delivers(pilot: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """G3: the worktree of a local-only repo delivers under a local repo id."""

    todo_id, root = _add_todo(pilot, ["api"], SINGLE_VALIDATION)
    worktree = root / "api"
    monkeypatch.chdir(worktree)
    monkeypatch.setenv("FAKE_CLAUDE_COMMIT_REPOS", ".")
    code, payload = _run_once(pilot, "dev", todo_id, worktree, SINGLE_VALIDATION)
    assert code == 0, json.dumps(payload)[:4000]
    assert payload["status"] == "committed"
    assert payload["effects"]["quota_spent"] is True
    assert _status(pilot, todo_id) == "in_review"
    snapshot = _delivery_workspaces(pilot)[-1]
    # A single-repo delivery keeps its shape; only the repository id is local.
    assert set(snapshot) == {
        "schema_version", "workspace_identity", "identity_kind", "task_repository",
        "workspace_revision_digest", "repository_source", "workspace_kind",
        "peer_independent_worktree_required",
    }
    assert snapshot["identity_kind"] == "git_repository"
    assert snapshot["workspace_kind"] == "independent_git_worktree"
    assert snapshot["task_repository"].startswith("local:")
    assert snapshot["repository_source"] == "current_git_common_dir"
