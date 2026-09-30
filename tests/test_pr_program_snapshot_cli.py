"""The PR-program snapshot diff script as a process: argv, files, stdout, exit codes."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "skills" / "loopx-pr-program" / "scripts" / "diff_snapshot.py"


def _snapshot(*, draft: bool, head: str, checks: str, review: str, work_item: str) -> dict[str, Any]:
    return {
        "schema_version": "loopx_pr_program_snapshot_v0",
        "program_id": "example-runtime-reliability",
        "generated_at": "2026-08-11T01:00:00Z",
        "result_completeness": {
            "complete": True,
            "scope": {
                "repositories": ["example/runtime"],
                "states": ["open"],
                "authors": [],
                "time_window": {"since": None, "until": None},
            },
        },
        "requirements": [
            {"id": "runtime-controls", "title": "Expose runtime controls", "priority": "P0", "coverage": "partial"}
        ],
        "change_requests": [
            {
                "ref": "example/runtime#42",
                "title": "feat(runtime): expose control status",
                "state": "open",
                "draft": draft,
                "target_branch": "main",
                "head_sha": head * 40,
                "updated_at": "2026-08-11T01:00:00Z",
                "checks": checks,
                "review": review,
                "work_item": work_item,
                "theme": "runtime controls",
                "priority": "P0",
                "requirement_ids": ["runtime-controls"],
                "depends_on": [],
                "supersedes": [],
                "description_digest": "sha256:example-description-42",
                "review_digest": "sha256:example-review-42",
            }
        ],
    }


BASELINE = _snapshot(draft=True, head="a", checks="pending", review="pending", work_item="action_required")
CURRENT = _snapshot(draft=False, head="b", checks="passed", review="approved", work_item="passed")


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def _diff(tmp_path: Path, current: dict[str, Any]) -> dict[str, Any]:
    previous = _write(tmp_path / "baseline.json", BASELINE)
    current_path = _write(tmp_path / "current.json", current)
    completed = _run("--previous", str(previous), "--current", str(current_path))
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_complete_snapshot_advances_the_baseline_and_reports_changed_fields(tmp_path: Path) -> None:
    delta = _diff(tmp_path, CURRENT)

    assert delta["program_id"] == "example-runtime-reliability"
    assert delta["baseline_advance_allowed"] is True
    assert delta["scope_matches_previous"] is True
    assert delta["changed"] == [
        {
            "ref": "example/runtime#42",
            "changed_fields": ["draft", "head_sha", "checks", "review", "work_item"],
            "before": {"draft": True, "head_sha": "a" * 40, "checks": "pending",
                       "review": "pending", "work_item": "action_required"},
            "after": {"draft": False, "head_sha": "b" * 40, "checks": "passed",
                      "review": "approved", "work_item": "passed"},
        }
    ]


def test_incomplete_snapshot_blocks_the_baseline(tmp_path: Path) -> None:
    incomplete = copy.deepcopy(CURRENT)
    incomplete["result_completeness"]["complete"] = False
    for field in ("checks", "review", "work_item"):
        incomplete["change_requests"][0][field] = "unknown"

    delta = _diff(tmp_path, incomplete)

    assert delta["baseline_advance_allowed"] is False
    assert delta["baseline_block_reason"] == "incomplete_result"
    assert delta["result_hash"] is None


def test_scope_mismatch_blocks_the_baseline(tmp_path: Path) -> None:
    mismatched = copy.deepcopy(CURRENT)
    mismatched["result_completeness"]["scope"]["repositories"] = ["example/other"]

    delta = _diff(tmp_path, mismatched)

    assert delta["baseline_advance_allowed"] is False
    assert delta["baseline_block_reason"] == "scope_mismatch"
    assert delta["result_hash"] is None


def test_output_file_receives_the_delta_instead_of_stdout(tmp_path: Path) -> None:
    current = _write(tmp_path / "current.json", CURRENT)
    output = tmp_path / "delta.json"

    completed = _run("--current", str(current), "--output", str(output))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert json.loads(output.read_text(encoding="utf-8"))["program_id"] == "example-runtime-reliability"


def test_invalid_invocations_exit_non_zero(tmp_path: Path) -> None:
    missing_current = _run("--previous", str(_write(tmp_path / "baseline.json", BASELINE)))
    assert missing_current.returncode == 2
    assert "--current" in missing_current.stderr

    other_program = copy.deepcopy(CURRENT)
    other_program["program_id"] = "another-program"
    mismatch = _run(
        "--previous", str(tmp_path / "baseline.json"),
        "--current", str(_write(tmp_path / "other.json", other_program)),
    )
    assert mismatch.returncode != 0
    assert "different program_id" in mismatch.stderr
    assert mismatch.stdout == ""
