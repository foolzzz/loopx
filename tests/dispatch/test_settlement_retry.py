"""A settlement that exhausts its retries keeps its Turn identity (G5 follow-up).

Quota spend and refresh-state retry run-index and state-file races a bounded
number of times. When a race outlasts them, run-once journals the Turn as
failed in a settlement phase after the host already completed. The
dispatcher must relaunch that same Turn, so the resume settles the cached
host result instead of minting a new Turn: the host does not redo its work
and the quota spend is not lost.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from loopx.control_plane.quota.spend_commit import QUOTA_SPEND_INDEX_CONFLICT_ATTEMPTS
from loopx.control_plane.turn_driver.journal_store import turn_journal_path
from loopx.dispatch import DispatchConfig, Dispatcher, policy, settlement_retry
from loopx.dispatch.state import load_state, save_state
from loopx.todos import add_goal_todo, list_goal_todos
from tests.dispatch.dispatch_fixtures import (
    GOAL_ID,
    git,
    git_env,
    make_repo,
    read_jsonl,
    write_fixture,
)
from tests.dispatch.test_loopx_dispatcher import Clock, ScriptedShouldRun, _dispatcher

ROOT = Path(__file__).resolve().parents[2]

# Runs the real `loopx.cli`, with quota spend raising the run-index conflict
# for as long as the contention marker file exists.
CONTENDED_LOOPX = r'''
import os, pathlib, sys
import loopx.cli_commands.turn as turn
from loopx.control_plane.quota import spend_commit
from loopx.control_plane.quota.spend_commit import QuotaSpendIndexConflict

marker = pathlib.Path(os.environ["FIXTURE_SPEND_CONTENTION"])
real_spend = turn.spend_quota_slot

def contended_spend(*args, **kwargs):
    if marker.exists():
        with open(str(marker) + ".attempts", "a", encoding="utf-8") as handle:
            handle.write("x")
        raise QuotaSpendIndexConflict("fixture: the run index moved under the preview")
    return real_spend(*args, **kwargs)

turn.spend_quota_slot = contended_spend
spend_commit.time.sleep = lambda _seconds: None
from loopx.cli import main
raise SystemExit(main())
'''


def _host_calls(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    return read_jsonl(fixture["claude_log"])


def _journal(fixture: dict[str, Any], turn_key: str) -> dict[str, Any]:
    path = turn_journal_path(fixture["runtime"], goal_id=GOAL_ID, turn_key=turn_key)
    return json.loads(path.read_text(encoding="utf-8"))


def _spends(fixture: dict[str, Any], todo_id: str) -> list[dict[str, Any]]:
    events = read_jsonl(fixture["runtime"] / "goals" / GOAL_ID / "rollout-event-log.jsonl")
    return [event for event in events if event.get("event_kind") == "quota_spend" and event.get("todo_id") == todo_id]


def test_settlement_retry_exhaustion_resumes_the_same_turn_without_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    api = make_repo(tmp_path, "api")
    git(api, "remote", "add", "origin", f"https://example.com/fixture/{api.name}.git")
    fixture = write_fixture(
        tmp_path,
        agents={
            "dev": {"role": "developer"},
            "acc": {"role": "acceptor", "runtime": "codex-cli"},
        },
        repos={"api": api},
    )
    todo_id = add_goal_todo(
        registry_path=fixture["registry"], goal_id=GOAL_ID, runtime_root_arg=str(fixture["runtime"]),
        role="agent", text="Build the api fixture", priority="P0",
        task_class="advancement_task", action_kind="fixture",
        role_contract={"task_repositories": ["api"]}, claimed_by="dev",
    )["todo_id"]
    wrapper = tmp_path / "contended_loopx.py"
    wrapper.write_text(CONTENDED_LOOPX, encoding="utf-8")
    marker = tmp_path / "spend-contention"
    marker.write_text("on", encoding="utf-8")
    validator = (sys.executable, "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('fixture-artifact.txt').exists() else 3)")
    clock = Clock()
    config = DispatchConfig(
        registry_path=fixture["registry"],
        runtime_root=fixture["runtime"],
        goal_ids=[GOAL_ID],
        no_global_sync=True,
        loopx_argv=(sys.executable, str(wrapper)),
        environ={
            **fixture["environ"],
            "PYTHONPATH": str(ROOT),
            "FIXTURE_SPEND_CONTENTION": str(marker),
            "FAKE_CLAUDE_RESULT_KIND": "validated_completion",
            "FAKE_CLAUDE_COMMIT": "1",
        },
        turn_timeout_seconds=180,
        default_validation_argv=validator,
    )
    should_run = ScriptedShouldRun({"dev": [todo_id], "acc": []})
    dispatcher = Dispatcher(config, should_run=should_run, clock=clock)

    first = dispatcher.run_once()
    assert [item["todo_id"] for item in first["launched"]] == [todo_id]
    turn_instance_id = first["launched"][0]["turn_instance_id"]
    assert [item["outcome"] for item in first["finished"]] == ["failed"]
    # Every bounded spend retry met the contention, after the host completed.
    assert len((tmp_path / "spend-contention.attempts").read_text()) == QUOTA_SPEND_INDEX_CONFLICT_ATTEMPTS
    assert len(_host_calls(fixture)) == 1
    state = load_state(fixture["runtime"])
    retry = state["retry_turns"][f"{GOAL_ID}/{todo_id}@dev"]
    assert retry["turn_instance_id"] == turn_instance_id
    assert retry["settlement"] is True and retry["failed_phase"] == "quota_spend"
    assert state["history"][-1]["settlement_retry"]["turn_key"] == retry["turn_key"]
    journal = _journal(fixture, retry["turn_key"])
    assert "typed_result" in journal["completed_phases"]
    assert "quota_spend" not in journal["completed_phases"]
    assert _spends(fixture, todo_id) == []

    # The failure backoff still applies: no hot relaunch of the same Turn.
    cooling = dispatcher.run_once()
    assert cooling["launched"] == []
    assert {"goal_id": GOAL_ID, "agent_id": "dev", "reason": "todo_cooldown", "todo_id": todo_id} in cooling["skipped"]

    # The contention clears. should-run no longer selects the delivered todo,
    # yet the dispatcher resumes the same Turn and run-once skips the host.
    marker.unlink()
    clock.now += 3600
    second = dispatcher.run_once()
    assert [(item["todo_id"], item["reason"]) for item in second["launched"]] == [(todo_id, "settlement_retry")]
    assert second["launched"][0]["turn_instance_id"] == turn_instance_id
    assert second["launched"][0]["turn_instance_reused"] is True
    history = load_state(fixture["runtime"])["history"]
    assert [item["outcome"] for item in second["finished"]] == ["committed"], Path(
        history[-1]["stdout_path"]
    ).read_text(encoding="utf-8")[:3000]
    resumed = json.loads(Path(history[-1]["stdout_path"]).read_text(encoding="utf-8"))
    assert resumed["resume_turn_key"] == retry["turn_key"]
    assert resumed["quota_slot_spend_count"] == 1
    assert len(_host_calls(fixture)) == 1, "the resume must not invoke the host again"
    assert "quota_spend" in _journal(fixture, retry["turn_key"])["completed_phases"]
    assert len(_spends(fixture, todo_id)) == 1
    assert load_state(fixture["runtime"])["retry_turns"] == {}
    rows = list_goal_todos(
        registry_path=fixture["registry"], goal_id=GOAL_ID, role="agent",
        runtime_root_arg=str(fixture["runtime"]),
    )["todos"]
    assert {row["todo_id"]: row["status"] for row in rows}[todo_id] == "in_review"


# --- classification and bookkeeping -------------------------------------------------

TURN_KEY = "sha256:" + "5" * 64


def _write_journal(runtime: Path, **fields: Any) -> None:
    path = turn_journal_path(runtime, goal_id=GOAL_ID, turn_key=TURN_KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "loopx_turn_journal_v0", "turn_key": TURN_KEY, **fields}))


@pytest.mark.parametrize(
    ("journal", "expected_phase"),
    [
        # A settlement effect raised: the journal is left in progress.
        ({"status": "in_progress", "completed_phases": ["host_execute", "typed_result", "validation"],
          "effect_attempts": {"durable_writeback": {"status": "prepared"}}}, "durable_writeback"),
        ({"status": "failed", "completed_phases": ["host_execute", "typed_result", "validation", "durable_writeback"],
          "receipt": {"failed_phase": "quota_spend"}}, "quota_spend"),
        # Validation and host failures redo the host by design: a new attempt.
        ({"status": "failed", "completed_phases": ["host_execute", "typed_result"],
          "receipt": {"failed_phase": "validation"}}, None),
        ({"status": "failed", "completed_phases": [], "receipt": {"failed_phase": "host_execute"}}, None),
        ({"status": "committed", "completed_phases": ["host_execute", "typed_result"]}, None),
    ],
)
def test_unsettled_turn_needs_a_cached_host_result_and_a_settlement_failure(
    tmp_path: Path, journal: dict[str, Any], expected_phase: str | None
) -> None:
    _write_journal(tmp_path, **journal)
    stdout = json.dumps({"ok": False, "status": journal["status"], "resume_turn_key": TURN_KEY})
    found = settlement_retry.unsettled_turn(tmp_path, GOAL_ID, stdout)
    assert (found or {}).get("failed_phase") == expected_phase
    if expected_phase:
        assert found == {"turn_key": TURN_KEY, "failed_phase": expected_phase}
    # A payload without a journal key, or a committed one, never keeps a Turn.
    assert settlement_retry.unsettled_turn(tmp_path, GOAL_ID, json.dumps({"ok": False})) is None
    assert settlement_retry.unsettled_turn(tmp_path, GOAL_ID, "Traceback") is None


def test_settlement_retries_stop_at_the_limit(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer"}})
    _write_journal(
        fixture["runtime"], status="failed", completed_phases=["host_execute", "typed_result"],
        receipt={"failed_phase": "quota_spend"},
    )
    dispatcher = _dispatcher(fixture)
    stdout = tmp_path / "run.out.json"
    stdout.write_text(json.dumps({"ok": False, "status": "failed", "resume_turn_key": TURN_KEY}))
    run = {"goal_id": GOAL_ID, "agent_id": "dev", "todo_id": "todo_aaa", "turn_instance_id": "dispatch:t1",
           "stdout_path": str(stdout), "project": str(tmp_path)}
    key = f"{GOAL_ID}/todo_aaa@dev"
    kept = dispatcher._settle_run("r1", {**run, "settlement_retries": 0}, 1)
    assert kept["outcome"] == policy.OUTCOME_FAILED
    assert dispatcher.state["retry_turns"][key]["settlement_retries"] == 1
    assert dispatcher.state["retry_turns"][key]["project"] == str(tmp_path)
    dispatcher.state["retry_turns"].clear()
    limit = settlement_retry.SETTLEMENT_RETRY_LIMIT
    dispatcher._settle_run("r2", {**run, "settlement_retries": limit}, 1)
    assert key not in dispatcher.state["retry_turns"]
    assert dispatcher.state["history"][-1]["settlement_retry"]["exhausted"] is True


def test_a_launch_keeps_the_legacy_retry_of_another_todo(tmp_path: Path) -> None:
    """The todo-less agent key holds pre-per-todo crash identities; only its own todo takes it."""

    fixture = write_fixture(tmp_path, agents={"dev": {"role": "developer", "max_concurrency": 2}})
    dispatcher = _dispatcher(fixture, should_run=ScriptedShouldRun({"dev": ["todo_bbb"]}))
    legacy = {"turn_instance_id": "dispatch:legacy-aaa", "todo_id": "todo_aaa", "crashes": 1}
    dispatcher.state.setdefault("retry_turns", {})[f"{GOAL_ID}/dev"] = dict(legacy)
    save_state(fixture["runtime"], dispatcher.state)
    report = dispatcher.run_once()
    assert [item["todo_id"] for item in report["launched"]] == ["todo_bbb"]
    assert report["launched"][0]["turn_instance_reused"] is False
    assert load_state(fixture["runtime"])["retry_turns"] == {f"{GOAL_ID}/dev": legacy}

    # The legacy identity's own todo takes it over and consumes it.
    dispatcher._should_run = ScriptedShouldRun({"dev": ["todo_aaa"]})
    report = dispatcher.run_once()
    assert report["launched"][0]["turn_instance_id"] == "dispatch:legacy-aaa"
    assert load_state(fixture["runtime"])["retry_turns"] == {}
