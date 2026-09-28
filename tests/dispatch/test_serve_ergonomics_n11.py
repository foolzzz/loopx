"""Pilot v1 gap N11: orchestrator supersede without the claim owner, and an idle heartbeat."""
from __future__ import annotations

import contextlib
import io
import json
import plistlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control_plane"))

from test_gates_plans_intake import ACC, DEV, GOAL, ORCH, fixture, rows  # noqa: E402

from loopx.cli import main  # noqa: E402
from loopx.dispatch.serve_heartbeat import DISPATCH_IDLE_HEARTBEAT_SCHEMA_VERSION, IdleHeartbeat  # noqa: E402
from loopx.rollout_event_log import load_rollout_events, rollout_event_log_path  # noqa: E402
from loopx.todos import add_goal_todo  # noqa: E402


def _cli(*argv: str) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(list(argv))
    return code, json.loads(out.getvalue())


def test_the_orchestrator_supersedes_another_agents_todo_as_its_claim_owner(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    base = ["--registry", str(registry), "--runtime-root", str(runtime), "--format", "json", "todo", "supersede",
            "--goal-id", GOAL]
    old = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Old work", claimed_by=DEV)["todo_id"]
    other = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Other", claimed_by=DEV)["todo_id"]

    # Another non-owner is still refused by the kernel claim fence.
    code, refused = _cli(*base, "--todo-id", other, "--agent-id", ACC, "--reason", "not mine")
    assert code == 1 and "claimed_by='dev'" in refused["error"]
    assert rows(registry)[other]["status"] == "open"

    code, payload = _cli(*base, "--todo-id", old, "--agent-id", ORCH, "--reason", "replaced by a split")
    assert code == 0 and payload["ok"] is True and payload["superseded"] is True
    row = rows(registry)[old]
    assert (row["status"], row["note"]) == ("done", "superseded")
    events = [e for e in load_rollout_events(rollout_event_log_path(runtime, GOAL))
              if e["event_kind"] == "todo_supersede" and e.get("todo_id") == old]
    assert len(events) == 1 and events[0]["agent_id"] == ORCH


def test_peer_v1_orchestrator_supersede_keeps_the_claim_rule(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path, agent_model="peer_v1")
    old = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Old work", claimed_by=DEV)["todo_id"]
    code, refused = _cli("--registry", str(registry), "--runtime-root", str(runtime), "--format", "json", "todo",
                         "supersede", "--goal-id", GOAL, "--todo-id", old, "--agent-id", ORCH, "--reason", "x")
    assert code == 1 and "claimed_by='dev'" in refused["error"]


def test_idle_heartbeat_is_low_rate_and_resets_on_a_logged_pass() -> None:
    clock = {"now": 0.0}
    heartbeat = IdleHeartbeat(900, clock=lambda: clock["now"])
    idle_pass = {"launched": [], "reaped": [], "errors": [], "gates_opened": [],
                 "skipped": [{"agent_id": "dev", "reason": "no_selected_todo"}]}
    lines = []
    for _ in range(60):  # one pass a minute for an hour
        clock["now"] += 60
        line = heartbeat.observe(idle_pass, running_turns=0)
        if line:
            lines.append(line)
    assert len(lines) == 4
    assert lines[0]["schema_version"] == DISPATCH_IDLE_HEARTBEAT_SCHEMA_VERSION
    assert (lines[0]["passes"], lines[0]["skip_reasons"]) == (15, {"no_selected_todo": 15})

    clock["now"] += 800
    assert heartbeat.observe({**idle_pass, "launched": [{"agent_id": "dev"}]}) is None
    clock["now"] += 800
    assert heartbeat.observe(idle_pass) is None  # the logged pass restarted the interval
    assert IdleHeartbeat(0, clock=lambda: 10**9).observe(idle_pass) is None


def test_launchd_plist_carries_the_heartbeat_interval(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["--registry", str(registry), "--runtime-root", str(runtime), "dispatch", "launchd-plist",
                     "--goal-id", GOAL, "--idle-heartbeat-seconds", "600"])
    assert code == 0
    args = plistlib.loads(out.getvalue().encode("utf-8"))["ProgramArguments"]
    assert args[args.index("--idle-heartbeat-seconds") + 1] == "600.0"


def test_dispatch_serve_prints_the_idle_heartbeat(tmp_path: Path, monkeypatch) -> None:
    import time

    from loopx.dispatch import Dispatcher

    idle_pass = {"launched": [], "reaped": [], "errors": [], "gates_opened": [],
                 "skipped": [{"agent_id": "orch", "reason": "should_run_false"}]}

    def fake_serve(self, *, stop, on_pass=None):  # noqa: ANN001 - test double for the resident loop
        for _ in range(2):
            time.sleep(0.02)
            on_pass(dict(idle_pass))

    monkeypatch.setattr(Dispatcher, "serve", fake_serve)
    registry, runtime = fixture(tmp_path)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["--registry", str(registry), "--runtime-root", str(runtime), "dispatch", "serve",
                     "--goal-id", GOAL, "--idle-heartbeat-seconds", "0.01"])
    assert code == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert lines and all(line["schema_version"] == DISPATCH_IDLE_HEARTBEAT_SCHEMA_VERSION for line in lines)
    assert lines[0]["skip_reasons"] == {"should_run_false": 1} and lines[0]["running_turns"] == 0
