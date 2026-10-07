"""Failed candidate checks stay visible without exposing doctor paths or output."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX local installer")
REQUIRED = (
    "command_package_same_root",
    "representative_cli_commands",
    "representative_cli_imports",
    "representative_package_paths",
)


def doctor_payload():
    return {
        "mode": "deep",
        "ok": True,
        "checks": [{"id": name, "required": True, "ok": True} for name in REQUIRED],
    }


def validate_candidate(tmp_path, payload, *, exit_code=0, raw_output=None):
    script = (
        Path(__file__).resolve().parents[1] / "scripts/install-local.sh"
    ).read_text()
    validator = (
        script.split("validate_release_candidate() {", 1)[1]
        .split("\nresolve_default_promotion()", 1)[0]
        .rsplit("\n}", 1)[0]
    )
    # Exercise the shipped failure cleanup before the default-link write.
    rejection = script.split('if ! validate_release_candidate "$release_dir"; then', 1)[
        1
    ].split("\nfi", 1)[0]
    candidate = tmp_path / "candidate"
    scripts = candidate / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / "loopx"
    wrapper.write_text(
        '#!/bin/sh\nprintf "%s" "$TEST_DOCTOR_OUTPUT"\nexit "$TEST_DOCTOR_EXIT"\n'
    )
    wrapper.chmod(0o755)
    default = tmp_path / "loopx"
    default.symlink_to("previous-release")
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOPX_")}
    env.update(
        HOME=str(tmp_path),
        CODEX_HOME=str(tmp_path / ".codex"),
        LOOPX_PYTHON=sys.executable,
        TEST_DOCTOR_OUTPUT=json.dumps(payload) if raw_output is None else raw_output,
        TEST_DOCTOR_EXIT=str(exit_code),
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -eu\nvalidate_release_candidate() {"
            + validator
            + '\n}\nrelease_dir="$1"\nif ! validate_release_candidate "$release_dir"; then'
            + rejection
            + '\nfi\nln -sfn "$release_dir/scripts/loopx" "$2"',
            "candidate-test",
            str(candidate),
            str(default),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, candidate, default


@pytest.mark.parametrize("exit_code", [0, 1])
def test_failed_required_check_is_reported_before_candidate_cleanup(
    tmp_path, exit_code
):
    payload = doctor_payload()
    payload["ok"] = False
    payload["checks"][1]["ok"] = False
    payload["checks"][1]["detail"] = "private-probe-output"
    payload["path"] = {"loopx": str(tmp_path / "private-path")}
    result, candidate, default = validate_candidate(
        tmp_path, payload, exit_code=exit_code
    )
    assert result.returncode == 1
    assert "representative_cli_commands" in result.stderr
    assert "private-probe-output" not in result.stderr
    assert str(tmp_path) not in result.stderr
    assert not candidate.exists()
    assert os.readlink(default) == "previous-release"


def test_successful_candidate_passes_without_failure_summary(tmp_path):
    result, candidate, default = validate_candidate(tmp_path, doctor_payload())
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    assert candidate.exists()
    assert default.resolve() == candidate / "scripts/loopx"


@pytest.mark.parametrize(
    "payload",
    [[], None, {"checks": None}, {"checks": [None]}, {"checks": [{"id": []}]}],
)
def test_invalid_doctor_shape_has_safe_diagnostic(tmp_path, payload):
    result, candidate, default = validate_candidate(tmp_path, payload, exit_code=1)
    assert result.returncode == 1
    assert "doctor output is invalid" in result.stderr
    assert "Traceback" not in result.stderr
    assert not candidate.exists()
    assert os.readlink(default) == "previous-release"


@pytest.mark.parametrize("raw_output", ["", "private-invalid-json"])
def test_invalid_json_is_not_echoed(tmp_path, raw_output):
    result, _, _ = validate_candidate(
        tmp_path, None, exit_code=2, raw_output=raw_output
    )
    assert result.returncode == 1
    assert "doctor output is invalid (doctor_exit=2)" in result.stderr
    assert "private-invalid-json" not in result.stderr


@pytest.mark.parametrize(
    "case", ["nonzero", "missing", "wrong_mode", "runtime", "unknown_check"]
)
def test_candidate_still_rejects_each_invalid_condition(tmp_path, case):
    payload = doctor_payload()
    exit_code = 0
    expected = "release candidate is incomplete"
    if case == "nonzero":
        exit_code = 7
        expected = "doctor_exit=7"
    elif case == "missing":
        payload["checks"].pop()
        expected = "missing_checks=['representative_package_paths']"
    elif case == "wrong_mode":
        payload["mode"] = "standard"
    else:
        # Even an inconsistent top-level ok cannot hide a required failure.
        check_id = (
            "typescript_effect_runtime_ready"
            if case == "runtime"
            else "private-check-id"
        )
        payload["checks"].append({"id": check_id, "required": True, "ok": False})
        payload["typescript_control_plane"] = {
            "status": "probe_failed",
            "semantic_probe": "failed",
            "runtime_lifecycle": {"diagnostic_code": "runtime_startup_timeout"},
        }
        expected = (
            "typescript_status=probe_failed semantic_probe=failed"
            if case == "runtime"
            else "unrecognized_check"
        )
    result, candidate, default = validate_candidate(
        tmp_path, payload, exit_code=exit_code
    )
    assert result.returncode == 1
    assert expected in result.stderr
    assert "private-check-id" not in result.stderr
    assert not candidate.exists()
    assert os.readlink(default) == "previous-release"
    if case == "runtime":
        assert "diagnostic_code=runtime_startup_timeout" in result.stderr


def test_failed_probe_summary_distinguishes_timeout_and_exit_without_raw_output(
    tmp_path,
):
    payload = doctor_payload()
    payload["ok"] = False
    payload["checks"][1]["ok"] = False
    payload["release_candidate"] = {
        "representative_cli": {
            "commands": {
                "results": {
                    "version": {
                        "ok": False,
                        "detail": "TimeoutExpired: private-command",
                    },
                    "commands": {
                        "ok": False,
                        "returncode": 3,
                        "stderr": "private-stderr",
                    },
                    "status_help": {"ok": True, "returncode": 0},
                    "private-probe-name": {"ok": False, "detail": "private-error"},
                }
            }
        }
    }
    result, _, _ = validate_candidate(tmp_path, payload, exit_code=1)
    assert "command_probe=version timeout" in result.stderr
    assert "command_probe=commands exit=3" in result.stderr
    assert "status_help" not in result.stderr
    assert "private-" not in result.stderr


def test_optional_failure_does_not_block_candidate(tmp_path):
    payload = doctor_payload()
    payload["checks"].append({"id": "optional_check", "required": False, "ok": False})
    result, _, _ = validate_candidate(tmp_path, payload)
    assert result.returncode == 0, result.stderr


def test_unknown_runtime_fields_are_redacted_without_masking_check_failure(tmp_path):
    payload = doctor_payload()
    payload["ok"] = False
    payload["checks"].append(
        {"id": "typescript_effect_runtime_ready", "required": True, "ok": False}
    )
    payload["typescript_control_plane"] = {
        "status": {"private-status": True},
        "semantic_probe": ["private-probe"],
        "runtime_lifecycle": {"diagnostic_code": "private-code"},
    }
    result, _, _ = validate_candidate(tmp_path, payload, exit_code=1)
    assert "typescript_effect_runtime_ready" in result.stderr
    assert (
        "typescript_status=unrecognized semantic_probe=unrecognized diagnostic_code=unrecognized"
        in result.stderr
    )
    assert "private-" not in result.stderr
    assert "Traceback" not in result.stderr
