"""G9: usage ledger idempotency, estimated pricing, report aggregation, budget."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from loopx.agent_config import load_providers
from loopx.agent_config.errors import AgentConfigError
from loopx.agent_config.pricing import TokenRates, estimate_cost_usd
from loopx.cli import main as cli_main
from loopx.control_plane.turn_driver.turn_usage import claude_code_turn_usage, codex_cli_turn_usage
from loopx.usage_accounting import (
    aggregate_usage,
    budget_status,
    goal_spend_usd,
    parse_since,
    read_usage_entries,
    record_turn_usage,
    usage_ledger_path,
    write_usage_budget,
)
from tests.test_loopx_turn_driver import _write_live_fixture
from tests.usage_accounting.test_turn_usage_capture import (
    CLAUDE_RESULT_ENVELOPE,
    CODEX_FRESH_EVENTS,
    CODEX_RESUMED_COMPLETED,
    FAKE_CLAUDE_USAGE,
    _fake_bin,
)
from loopx.control_plane.turn_driver.turn_usage import codex_event_token_usage

GOAL = "usage-goal"
PRICES = (
    "providers:\n"
    "  cpa:\n"
    "    kind: codex-cpa\n"
    "    auth: {type: api_key}\n"
    "    pricing:\n"
    "      input: 1.0\n"
    "      cached_input: 0.1\n"
    "      output: 10\n"
    "      models:\n"
    "        gpt-5.6-sol: {input: 2.0, cached_input: 0.2, output: 20}\n"
)


def _claude_usage(started: float = 1_790_000_000.0, seconds: float = 1800.0) -> dict:
    return claude_code_turn_usage(CLAUDE_RESULT_ENVELOPE, started_at=started, finished_at=started + seconds)


def _codex_usage(event: dict, *, resumed: bool, started: float = 1_790_000_000.0) -> dict:
    return codex_cli_turn_usage(
        codex_event_token_usage(event), started_at=started, finished_at=started + 600,
        model="gpt-5.6-sol", session_id="thread-a", resumed=resumed,
    )


def test_pricing_parses_estimates_and_rejects_bad_tables(tmp_path: Path) -> None:
    (tmp_path / "providers.yaml").write_text(PRICES, encoding="utf-8")
    pricing = load_providers(tmp_path)["cpa"].pricing
    assert pricing.rates_for("gpt-5.6-sol").output == 20
    assert pricing.rates_for("other").input == 1.0
    tokens = {"input": 1_000_000, "cached_input": 2_000_000, "cache_creation_input": 0, "output": 100_000}
    assert estimate_cost_usd(tokens, pricing.rates_for("other")) == pytest.approx(1.0 + 0.2 + 1.0)
    assert estimate_cost_usd(tokens, None) is None
    assert estimate_cost_usd(
        {"input": 0, "cache_creation_input": 1_000_000},
        TokenRates(input=3.0, cached_input=0.3, output=15.0),
    ) == pytest.approx(3.0)

    (tmp_path / "providers.yaml").write_text(
        "providers:\n  cpa:\n    kind: codex-cpa\n    auth: {type: api_key}\n"
        "    pricing: {input: 1.0, output: -2}\n",
        encoding="utf-8",
    )
    with pytest.raises(AgentConfigError) as exc_info:
        load_providers(tmp_path)
    assert "cached_input" in str(exc_info.value)


def test_ledger_is_idempotent_per_turn_attempt_and_counts_retries(tmp_path: Path) -> None:
    usage = {**_claude_usage(), "host_attempt": 1}
    lineage = dict(agent_id="dev", turn_key="sha256:" + "a" * 64, role="developer",
                   todo_id="todo_a1", provider="anthropic-login", outcome="validated_progress",
                   status="committed")
    first = record_turn_usage(tmp_path, GOAL, usage, **lineage)
    replay = record_turn_usage(tmp_path, GOAL, usage, **lineage)
    retry = record_turn_usage(tmp_path, GOAL, {**usage, "host_attempt": 2}, **lineage)

    assert first["recorded"] is True and first["cost_source"] == "host_reported"
    assert replay == {"recorded": False, "duplicate": True, "entry_id": first["entry_id"]}
    assert retry["recorded"] is True and retry["entry_id"].endswith("#2")
    rows = read_usage_entries(tmp_path, GOAL)
    assert [row["entry_id"] for row in rows] == [first["entry_id"], retry["entry_id"]]
    row = rows[0]
    assert row["cost_estimated"] is False and row["role"] == "developer"
    assert row["model"] == "claude-haiku-4-5-20251001" and row["duration_ms"] == 1_800_000
    assert goal_spend_usd(tmp_path, GOAL) == pytest.approx(2 * 0.04702625)
    assert usage_ledger_path(tmp_path, GOAL) == tmp_path / "goals" / GOAL / "usage.jsonl"


def test_ledger_estimates_codex_cost_from_session_deltas(tmp_path: Path) -> None:
    (tmp_path / "providers.yaml").write_text(PRICES, encoding="utf-8")
    pricing = load_providers(tmp_path)["cpa"].pricing
    common = dict(agent_id="acc", role="acceptor", todo_id="todo_a1", provider="cpa", pricing=pricing)
    record_turn_usage(tmp_path, GOAL, _codex_usage(CODEX_FRESH_EVENTS[-1], resumed=False),
                      turn_key="sha256:" + "b" * 64, **common)
    record_turn_usage(tmp_path, GOAL, _codex_usage(CODEX_RESUMED_COMPLETED, resumed=True),
                      turn_key="sha256:" + "c" * 64, **common)
    fresh, resumed = read_usage_entries(tmp_path, GOAL)

    assert fresh["cost_estimated"] is True and fresh["cost_source"] == "estimated"
    assert fresh["cost_usd"] == pytest.approx((17977 * 2.0 + 5 * 20) / 1e6)
    # The resumed Turn counts only its own tokens, not the session total.
    assert resumed["tokens"]["input"] == 35971 - 17792 - 17977
    assert resumed["tokens"]["cached_input"] == 17792 and resumed["tokens"]["output"] == 5
    assert resumed["cost_usd"] == pytest.approx((202 * 2.0 + 17792 * 0.2 + 5 * 20) / 1e6)
    assert "cumulative_unresolved" not in resumed

    # Without a recorded previous Turn of the session, the over-count is flagged.
    orphan = record_turn_usage(tmp_path, "other-goal", _codex_usage(CODEX_RESUMED_COMPLETED, resumed=True),
                               turn_key="sha256:" + "d" * 64, **common)
    assert orphan["recorded"] is True
    assert read_usage_entries(tmp_path, "other-goal")[0]["cumulative_unresolved"] is True

    unpriced = record_turn_usage(tmp_path, "third-goal", _codex_usage(CODEX_FRESH_EVENTS[-1], resumed=False),
                                 turn_key="sha256:" + "e" * 64, agent_id="acc")
    assert unpriced["cost_source"] == "unpriced" and unpriced["cost_usd"] is None


def test_report_aggregates_by_role_day_model_and_accepted_todos(tmp_path: Path) -> None:
    (tmp_path / "providers.yaml").write_text(PRICES, encoding="utf-8")
    pricing = load_providers(tmp_path)["cpa"].pricing
    record_turn_usage(tmp_path, GOAL, _claude_usage(), agent_id="dev", role="developer", todo_id="todo_a1",
                      turn_key="sha256:" + "1" * 64, status="committed")
    record_turn_usage(tmp_path, GOAL, _claude_usage(seconds=900), agent_id="dev", role="developer",
                      todo_id="todo_b2", turn_key="sha256:" + "2" * 64, status="failed")
    record_turn_usage(tmp_path, GOAL, _codex_usage(CODEX_FRESH_EVENTS[-1], resumed=False, started=1_790_090_000.0),
                      agent_id="acc", role="acceptor", todo_id="todo_a1", provider="cpa", pricing=pricing,
                      turn_key="sha256:" + "3" * 64, status="committed")
    rows = read_usage_entries(tmp_path, GOAL)

    report = aggregate_usage(rows, by="role", accepted_todo_keys={f"{GOAL}/todo_a1"})
    totals = report["totals"]
    assert totals["turns"] == 3 and totals["failed_turns"] == 1
    assert totals["agent_hours"] == pytest.approx(0.5 + 0.25 + 600 / 3600, abs=1e-4)
    assert totals["cost_reported_usd"] == pytest.approx(2 * 0.04702625, abs=1e-6)
    assert totals["cost_estimated_usd"] == pytest.approx((17977 * 2.0 + 5 * 20) / 1e6, abs=1e-6)
    assert totals["accepted_todos"] == 1 and totals["todos"] == 2
    assert totals["cost_per_accepted_todo_usd"] == pytest.approx(totals["cost_usd"])
    assert totals["turns_per_accepted_todo"] == 3
    by_role = {group["key"]: group for group in report["groups"]}
    assert by_role["developer"]["turns"] == 2 and by_role["acceptor"]["cost_estimated_usd"] > 0

    days = aggregate_usage(rows, by="day")["groups"]
    assert len(days) == 2 and days[0]["key"] < days[1]["key"]
    models = {group["key"] for group in aggregate_usage(rows, by="model")["groups"]}
    assert models == {"claude-haiku-4-5-20251001", "gpt-5.6-sol"}
    since = datetime.fromtimestamp(1_790_050_000.0, tz=timezone.utc)
    assert aggregate_usage(rows, since=since)["totals"]["turns"] == 1
    assert parse_since("2026-09-01") is not None
    with pytest.raises(ValueError):
        parse_since("2026-09-01", days=3)
    with pytest.raises(ValueError):
        aggregate_usage(rows, by="provider")


def test_budget_status_thresholds(tmp_path: Path) -> None:
    assert budget_status(tmp_path, GOAL) is None
    record_turn_usage(tmp_path, GOAL, _claude_usage(), agent_id="dev", turn_key="sha256:" + "f" * 64)
    write_usage_budget(tmp_path, GOAL, 0.05)
    status = budget_status(tmp_path, GOAL)
    assert status["crossed"] == [0.8] and status["spent_ratio"] == pytest.approx(0.9405, abs=1e-3)
    write_usage_budget(tmp_path, GOAL, 0.04)
    assert budget_status(tmp_path, GOAL)["crossed"] == [0.8, 1.0]
    with pytest.raises(ValueError):
        write_usage_budget(tmp_path, GOAL, -1)
    write_usage_budget(tmp_path, GOAL, None)
    assert budget_status(tmp_path, GOAL) is None


def _run_cli(argv: list[str]) -> tuple[int, dict]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli_main(argv)
    return code, json.loads(output.getvalue())


def test_turn_run_once_records_usage_once_and_report_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, runtime, registry = _write_live_fixture(tmp_path)
    executable = _fake_bin(tmp_path, "claude", FAKE_CLAUDE_USAGE)
    monkeypatch.setenv("FAKE_CLAUDE_ENVELOPE", json.dumps(CLAUDE_RESULT_ENVELOPE))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    base = ["--registry", str(registry), "--runtime-root", str(runtime), "--format", "json"]

    code, payload = _run_cli(
        [
            *base, "turn", "run-once", "--host", "claude-code",
            "--goal-id", "loopx-turn-fixture", "--agent-id", "codex-fixture",
            "--project", str(workspace), "--claude-bin", str(executable), "--claude-model", "haiku",
            "--validation-command-json", json.dumps([sys.executable, "-c", "import sys; sys.stdin.read()"]),
            "--scan-root", str(project), "--no-global-sync", "--execute",
        ]
    )
    assert code == 0, payload
    assert payload["status"] == "committed"
    assert payload["turn_usage"]["cost_usd"] == pytest.approx(0.04702625)
    assert payload["turn_usage"]["host_attempt"] == 1
    assert payload["usage_ledger"]["recorded"] is True
    rows = read_usage_entries(runtime, "loopx-turn-fixture")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "validated_progress" and rows[0]["status"] == "committed"
    assert rows[0]["agent_id"] == "codex-fixture" and rows[0]["host"] == "claude-code"

    # Replaying the settled Turn re-reads the journaled usage without a second row.
    code, replay = _run_cli(
        [
            *base, "turn", "run-once", "--host", "claude-code",
            "--goal-id", "loopx-turn-fixture", "--agent-id", "codex-fixture",
            "--resume-turn-key", payload["resume_turn_key"],
            "--project", str(workspace), "--claude-bin", str(executable),
            "--scan-root", str(project), "--no-global-sync", "--execute",
        ]
    )
    assert replay["effects"]["host_invoked"] is False
    assert replay.get("usage_ledger", {}).get("recorded") in (None, False)
    assert len(read_usage_entries(runtime, "loopx-turn-fixture")) == 1

    code, report = _run_cli([*base, "usage", "report", "--goal", "loopx-turn-fixture", "--by", "agent"])
    assert code == 0
    assert report["totals"]["turns"] == 1
    assert report["totals"]["cost_reported_usd"] == pytest.approx(0.04702625, abs=1e-6)
    assert report["groups"][0]["key"] == "codex-fixture"
    code, cross = _run_cli([*base, "usage", "report", "--by", "day"])
    assert code == 0 and cross["scope"] == "registry" and cross["goal_ids"] == ["loopx-turn-fixture"]


def test_turn_run_once_records_usage_of_a_failed_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, runtime, registry = _write_live_fixture(tmp_path)
    executable = _fake_bin(tmp_path, "claude", FAKE_CLAUDE_USAGE)
    monkeypatch.setenv("FAKE_CLAUDE_ENVELOPE", json.dumps(CLAUDE_RESULT_ENVELOPE))
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    code, payload = _run_cli(
        [
            "--registry", str(registry), "--runtime-root", str(runtime), "--format", "json",
            "turn", "run-once", "--host", "claude-code",
            "--goal-id", "loopx-turn-fixture", "--agent-id", "codex-fixture",
            "--project", str(workspace), "--claude-bin", str(executable),
            "--scan-root", str(project), "--no-global-sync", "--execute",
        ]
    )
    assert code == 1
    assert payload["host_failure"]["kind"] == "rate_limited"
    rows = read_usage_entries(runtime, "loopx-turn-fixture")
    assert len(rows) == 1
    assert rows[0]["failure_kind"] == "rate_limited" and rows[0]["status"] == "failed"
    assert rows[0]["cost_usd"] == pytest.approx(0.04702625)
