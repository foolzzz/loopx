from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from loopx.capabilities.reward_memory import refresh_outcome_candidate
from loopx.capabilities.reward_memory.refresh_outcome_candidate import (
    REFRESH_OUTCOME_CANDIDATE_SCHEMA_VERSION,
    REFRESH_OUTCOME_CANDIDATE_SIDECAR_SCHEMA_VERSION,
    refresh_outcome_candidate_sidecar_path,
    run_staged_refresh_outcome_candidate_ingest,
    stage_refresh_outcome_candidate,
)


def _reflection() -> str:
    evidence_refs = ["artifact:app-route", "receipt:app-validation"]
    return json.dumps(
        {
            "schema_version": "turn_reward_memory_reflection_v1",
            "status": "eligible",
            "surface_id": "agent_workflow.turn_admission",
            "outcome_kind": "engineering",
            "content_summary": "Run the exact admission test before changing routing.",
            "reasoning_summary": "The independently checked route avoided stale state.",
            "confidence": "high",
            "evidence_refs": evidence_refs,
            "experience": {
                "schema_version": "procedural_experience_contract_v0",
                "applicability": ["Changing a managed Turn admission route"],
                "observed_outcome": (
                    "The exact admission test avoided a route based on stale state."
                ),
                "attribution": (
                    "Independent validation bound the route result to current "
                    "admission evidence."
                ),
                "future_behavior": {
                    "trigger": "A managed Turn admission route may change.",
                    "action": "Run the exact admission test before changing routing.",
                    "validation": (
                        "Bind the result to the declared validator and current route "
                        "receipt."
                    ),
                    "stop_condition": (
                        "Do not change routing when the validator or current receipt "
                        "is missing."
                    ),
                },
                "limitations": [
                    "This result applies only to the validated route and revision."
                ],
                "evidence_refs": evidence_refs,
            },
        }
    )


def _large_but_valid_reflection() -> str:
    payload = json.loads(_reflection())
    experience = payload["experience"]
    payload["content_summary"] = "C" * 500
    payload["reasoning_summary"] = "R" * 500
    experience["applicability"] = ["A" * 500, "B" * 500]
    experience["observed_outcome"] = "O" * 500
    experience["attribution"] = "T" * 500
    experience["future_behavior"] = {
        "trigger": "G" * 500,
        "action": "X" * 500,
        "validation": "V" * 500,
        "stop_condition": "S" * 500,
    }
    experience["limitations"] = ["L" * 500, "M" * 500]
    serialized = json.dumps(payload)
    assert len(serialized) > 2_400
    return serialized


def _validator(path: Path, *, attest: bool) -> list[str]:
    body = (
        "import json,sys; r=json.load(sys.stdin); "
        + (
            "print(json.dumps({'schema_version':"
            "'reward_memory_reflection_validation_v0','status':'validated',"
            "'reflection_digest':r['reflection_digest'],"
            "'evidence_refs':r['reflection']['evidence_refs']}))"
            if attest
            else "print('validation passed without reflection attestation')"
        )
    )
    path.write_text(body + "\n", encoding="utf-8")
    return [sys.executable, str(path)]


def test_refresh_outcome_candidate_writes_new_schema_and_reads_legacy_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = _validator(tmp_path / "validate.py", attest=True)
    monkeypatch.setattr(
        refresh_outcome_candidate,
        "_validation_declaration",
        lambda **_kwargs: {
            "validation_command": None,
            "validation_command_argv": argv,
            "validation_label": "app reflection validator",
            "validation_timeout_seconds": 5,
        },
    )
    monkeypatch.setattr(
        refresh_outcome_candidate,
        "_goal_repo",
        lambda *_args, **_kwargs: tmp_path,
    )

    staged = stage_refresh_outcome_candidate(
        registry_path=tmp_path / "registry.json",
        runtime_root=tmp_path / "runtime",
        goal_id="goal",
        agent_id="pilot",
        todo_id="todo_app",
        turn_instance_id="turn-app",
        effect_id="effect:app",
        state_file=tmp_path / "ACTIVE_GOAL_STATE.md",
        validation_workspace=tmp_path,
        reflection_json=_reflection(),
        observed_at="2026-09-13T03:00:00+00:00",
    )

    assert staged["status"] == "validation_bound"
    assert staged["schema_version"] == REFRESH_OUTCOME_CANDIDATE_SCHEMA_VERSION
    assert staged["candidate_id"].startswith("refresh:")
    assert staged["validation_bound"] is True
    assert staged["raw_content_projected"] is False
    assert staged["external_writes_performed"] is False
    path = refresh_outcome_candidate_sidecar_path(
        tmp_path,
        goal_id="goal",
        agent_id="pilot",
        candidate_id=staged["candidate_id"],
    )
    assert path.stat().st_mode & 0o777 == 0o600
    sidecar = json.loads(path.read_text(encoding="utf-8"))
    assert sidecar["schema_version"] == REFRESH_OUTCOME_CANDIDATE_SIDECAR_SCHEMA_VERSION
    assert "codex_app" not in json.dumps(sidecar)

    legacy_root = tmp_path / ".local" / "loopx" / "reward-memory-app-outcomes"
    current_root = (
        tmp_path / ".local" / "loopx" / "reward-memory-refresh-outcome-candidates"
    )
    legacy_candidate_id = "app:legacy-candidate"
    legacy_current_path = refresh_outcome_candidate_sidecar_path(
        tmp_path,
        goal_id="goal",
        agent_id="pilot",
        candidate_id=legacy_candidate_id,
    )
    legacy_path = legacy_root / legacy_current_path.relative_to(current_root)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar["schema_version"] = "codex_app_reward_memory_outcome_sidecar_v0"
    sidecar["candidate_id"] = legacy_candidate_id
    legacy_path.write_text(json.dumps(sidecar), encoding="utf-8")
    path.unlink()

    observed: dict[str, Any] = {}

    def ingest(**kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {
            "ok": True,
            "status": "activated",
            "external_writes_performed": True,
        }

    monkeypatch.setattr(
        refresh_outcome_candidate,
        "run_configured_turn_outcome_ingest",
        ingest,
    )
    finalized = run_staged_refresh_outcome_candidate_ingest(
        registry_path=tmp_path / "registry.json",
        goal_id="goal",
        agent_id="pilot",
        todo_id="todo_app",
        turn_instance_id="turn-app",
        effect_id="effect:app",
        candidate_id=legacy_candidate_id,
        writeback_appended=True,
        spend_appended=True,
    )

    assert finalized["status"] == "activated"
    assert finalized["host_wiring"] == (
        "refresh_outcome_candidate_post_settlement"
    )
    assert observed["turn_key"] == "effect:app"
    evidence = observed["settlement_evidence"]
    assert evidence["task_validation"]["ok"] is True
    assert evidence["writeback"] == {"ok": True, "appended": True}
    assert evidence["quota_spend"] == {"ok": True, "appended": True}


def test_refresh_without_exact_validator_attestation_stays_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = _validator(tmp_path / "validate.py", attest=False)
    monkeypatch.setattr(
        refresh_outcome_candidate,
        "_validation_declaration",
        lambda **_kwargs: {
            "validation_command": None,
            "validation_command_argv": argv,
            "validation_label": "ordinary validator",
            "validation_timeout_seconds": 5,
        },
    )
    monkeypatch.setattr(
        refresh_outcome_candidate,
        "_goal_repo",
        lambda *_args, **_kwargs: tmp_path,
    )

    staged = stage_refresh_outcome_candidate(
        registry_path=tmp_path / "registry.json",
        runtime_root=tmp_path / "runtime",
        goal_id="goal",
        agent_id="pilot",
        todo_id="todo_app",
        turn_instance_id="turn-app",
        effect_id="effect:app",
        state_file=tmp_path / "ACTIVE_GOAL_STATE.md",
        validation_workspace=tmp_path,
        reflection_json=_reflection(),
        observed_at="2026-09-13T03:00:00+00:00",
    )

    assert staged["status"] == "awaiting_evidence_validation"
    assert staged["validation_bound"] is False
    assert staged["provider_sync_count"] == 0
    assert staged["external_writes_performed"] is False


def test_refresh_accepts_large_valid_reflection_without_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        refresh_outcome_candidate,
        "_validation_declaration",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        refresh_outcome_candidate,
        "_goal_repo",
        lambda *_args, **_kwargs: tmp_path,
    )

    staged = stage_refresh_outcome_candidate(
        registry_path=tmp_path / "registry.json",
        runtime_root=tmp_path / "runtime",
        goal_id="goal",
        agent_id="pilot",
        todo_id="todo_without_validator",
        turn_instance_id="turn-large-reflection",
        effect_id="effect:large-reflection",
        state_file=tmp_path / "ACTIVE_GOAL_STATE.md",
        validation_workspace=tmp_path,
        reflection_json=_large_but_valid_reflection(),
        observed_at="2026-09-21T15:02:02+00:00",
    )

    assert staged["status"] == "awaiting_evidence_validation"
    assert staged["reason_code"] == "reflection_not_bound_to_independent_validation"
    assert staged["candidate_id"]
    assert staged["reflection_digest"].startswith("sha256:")
    assert staged["validation_bound"] is False
    assert staged["provider_sync_count"] == 0
    assert staged["external_writes_performed"] is False


def test_refresh_rejects_truly_oversized_reflection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        refresh_outcome_candidate,
        "_goal_repo",
        lambda *_args, **_kwargs: tmp_path,
    )
    payload = json.loads(_reflection())
    payload["content_summary"] = "X" * (17 * 1024)

    with pytest.raises(
        ValueError,
        match="reward memory reflection exceeds its bounded contract",
    ):
        stage_refresh_outcome_candidate(
            registry_path=tmp_path / "registry.json",
            runtime_root=tmp_path / "runtime",
            goal_id="goal",
            agent_id="pilot",
            todo_id="todo_oversized",
            turn_instance_id="turn-oversized-reflection",
            effect_id="effect:oversized-reflection",
            state_file=tmp_path / "ACTIVE_GOAL_STATE.md",
            validation_workspace=tmp_path,
            reflection_json=json.dumps(payload),
            observed_at="2026-09-21T15:02:02+00:00",
        )
