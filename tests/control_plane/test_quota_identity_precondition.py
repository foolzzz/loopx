from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from loopx.cli import main
from loopx.control_plane.agents.identity import (
    build_identity_aware_prompt_upgrade,
    build_quota_agent_identity,
)
from loopx.control_plane.agents.runtime_model import RetiredAgentHierarchyError
from loopx.control_plane.quota.error_codes import (
    QuotaIdentityPrecondition,
    QuotaIdentityPreconditionError,
)
from loopx.control_plane.testing.canary_harness import (
    project_state_path,
    write_fixture_registry,
)


GOAL_ID = "quota-identity-precondition"
AGENT_ID = "agent-alpha"


def _write_state(project: Path) -> None:
    state_file = project_state_path(project, GOAL_ID)
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        "---\n"
        "status: active\n"
        "---\n\n"
        "# Quota identity precondition fixture\n\n"
        "## Objective\n\n"
        "Keep selected-registry identity admission explicit.\n\n"
        "## Next Action\n\n"
        "- Repair the selected registry roster.\n",
        encoding="utf-8",
    )


def test_selected_registry_missing_roster_returns_typed_public_recovery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    global_project = tmp_path / "global-project"
    runtime_root = tmp_path / "runtime"
    selected_registry = project / ".loopx" / "registry.json"
    global_registry = runtime_root / "registry.global.json"
    _write_state(project)
    _write_state(global_project)
    write_fixture_registry(
        project=project,
        runtime_root=runtime_root,
        registry_path=selected_registry,
        goal_id=GOAL_ID,
        domain="quota-identity-precondition",
        adapter_kind="generic_project_goal_v0",
        registered_agents=None,
        extra_goal_fields={"coordination": {"agent_model": "peer_v1"}},
    )
    write_fixture_registry(
        project=global_project,
        runtime_root=runtime_root,
        registry_path=global_registry,
        goal_id=GOAL_ID,
        domain="quota-identity-precondition",
        adapter_kind="generic_project_goal_v0",
        registered_agents=[AGENT_ID],
    )

    exit_code = main(
        [
            "--format",
            "json",
            "--registry",
            str(selected_registry),
            "--runtime-root",
            str(runtime_root),
            "quota",
            "should-run",
            "--goal-id",
            GOAL_ID,
            "--agent-id",
            AGENT_ID,
            "--scan-path",
            str(project),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["should_run"] is False
    assert payload["agent_id"] == AGENT_ID
    assert payload["error_code"] == "quota_agent_registry_roster_missing"
    assert payload["status"] == "quota_identity_precondition_failed"
    assert payload["identity_precondition"] == "registered_agent_roster_present"
    assert payload["reason"] == (
        "selected registry goal is missing coordination.registered_agents"
    )
    assert "selected registry" in payload["recommended_action"]
    assert "--agent-id" in payload["recommended_action"]
    assert "verbose_debug" not in payload


@pytest.mark.parametrize(
    ("goal", "agent_id", "precondition", "error_code", "public_agent_id"),
    [
        (
            {},
            "../private-agent",
            QuotaIdentityPrecondition.PUBLIC_SAFE_AGENT_ID,
            "quota_agent_id_invalid",
            None,
        ),
        (
            {"coordination": {"agent_model": "peer_v1"}},
            AGENT_ID,
            QuotaIdentityPrecondition.REGISTERED_AGENT_ROSTER_PRESENT,
            "quota_agent_registry_roster_missing",
            AGENT_ID,
        ),
        (
            {"coordination": {"registered_agents": ["agent-beta"]}},
            AGENT_ID,
            QuotaIdentityPrecondition.REQUESTED_AGENT_REGISTERED,
            "quota_agent_not_registered",
            AGENT_ID,
        ),
    ],
)
def test_quota_identity_admission_uses_typed_public_preconditions(
    goal: dict[str, object],
    agent_id: str,
    precondition: QuotaIdentityPrecondition,
    error_code: str,
    public_agent_id: str | None,
) -> None:
    with pytest.raises(QuotaIdentityPreconditionError) as exc_info:
        build_quota_agent_identity(goal, agent_id=agent_id)

    assert exc_info.value.precondition is precondition
    assert exc_info.value.error_code == error_code
    assert exc_info.value.agent_id == public_agent_id


@pytest.mark.parametrize(
    ("coordination", "legacy_field"),
    [
        ({"agent_model": "legacy_hierarchy"}, "coordination.agent_model"),
        ({"primary_agent": AGENT_ID}, "coordination.primary_agent"),
        (
            {"side_agent_handoff_agent": AGENT_ID},
            "coordination.side_agent_handoff_agent",
        ),
        (
            {
                "agent_profiles": {
                    AGENT_ID: {"schema_version": "agent_profile_v0"}
                }
            },
            f'coordination.agent_profiles["{AGENT_ID}"].schema_version',
        ),
        (
            {"agent_profiles": {AGENT_ID: {"primary_agent": "agent-beta"}}},
            f'coordination.agent_profiles["{AGENT_ID}"].primary_agent',
        ),
        (
            {"agent_profiles": {AGENT_ID: {"worktree_policy": "clean-worktree"}}},
            f'coordination.agent_profiles["{AGENT_ID}"].worktree_policy',
        ),
        (
            {
                "agent_profiles": {
                    AGENT_ID: {"review_policy": {"handoff_agent": "agent-beta"}}
                }
            },
            (
                f'coordination.agent_profiles["{AGENT_ID}"].review_policy.'
                "handoff_agent"
            ),
        ),
        (
            {
                "agent_profiles": {
                    AGENT_ID: {
                        "review_policy": {"reviews_side_agent_work": False}
                    }
                }
            },
            (
                f'coordination.agent_profiles["{AGENT_ID}"].review_policy.'
                "reviews_side_agent_work"
            ),
        ),
        (
            {
                "agent_model": "peer_v1",
                "agent_profiles": {AGENT_ID: {"role": "side-agent"}},
            },
            f'coordination.agent_profiles["{AGENT_ID}"].role',
        ),
        (
            {
                "agent_profiles": {
                    AGENT_ID: {"review_policy": {"can_self_merge": True}}
                }
            },
            f'coordination.agent_profiles["{AGENT_ID}"].review_policy.can_self_merge',
        ),
        (
            {
                "completed_migrations": {
                    "peer_agent_runtime_v1": {"status": "completed"}
                }
            },
            "coordination.completed_migrations.peer_agent_runtime_v1",
        ),
    ],
)
def test_retired_v01_hierarchy_fields_fail_fast_with_remediation(
    coordination: dict[str, object],
    legacy_field: str,
) -> None:
    goal = {
        "coordination": {
            "registered_agents": [AGENT_ID],
            **coordination,
        }
    }

    with pytest.raises(ValueError) as exc_info:
        build_quota_agent_identity(goal, agent_id=AGENT_ID)

    message = str(exc_info.value)
    assert "retired v0.1 agent hierarchy" in message
    assert legacy_field in message
    assert "remove the listed fields" in message
    assert "coordination.agent_model=role_v1 or peer_v1" in message


def test_root_legacy_model_alias_is_rejected() -> None:
    goal = {
        "agent_model": "legacy_hierarchy",
        "coordination": {
            "agent_model": "role_v1",
            "registered_agents": [AGENT_ID],
        },
    }

    with pytest.raises(ValueError, match="retired v0.1.*agent_model"):
        build_quota_agent_identity(goal, agent_id=AGENT_ID)


def test_root_role_v1_keeps_current_profile_roles() -> None:
    goal = {
        "agent_model": "role_v1",
        "coordination": {
            "registered_agents": [AGENT_ID],
            "agent_profiles": {
                AGENT_ID: {
                    "schema_version": "agent_profile_v1",
                    "role": "developer",
                }
            },
        },
    }

    identity = build_quota_agent_identity(goal, agent_id=AGENT_ID)

    assert identity is not None
    assert identity["agent_model"] == "role_v1"


def test_default_role_v1_rejects_profile_roles_without_explicit_model() -> None:
    goal = {
        "coordination": {
            "registered_agents": [AGENT_ID],
            "agent_profiles": {AGENT_ID: {"role": "developer"}},
        },
    }

    with pytest.raises(RetiredAgentHierarchyError) as exc_info:
        build_quota_agent_identity(goal, agent_id=AGENT_ID)

    assert f'coordination.agent_profiles["{AGENT_ID}"].role' in exc_info.value.fields


def test_coordination_model_overrides_root_model_for_legacy_role_detection() -> None:
    goal = {
        "agent_model": "role_v1",
        "coordination": {
            "agent_model": "peer_v1",
            "registered_agents": [AGENT_ID],
            "agent_profiles": {AGENT_ID: {"role": "side-agent"}},
        },
    }

    with pytest.raises(RetiredAgentHierarchyError) as exc_info:
        build_quota_agent_identity(goal, agent_id=AGENT_ID)

    assert f'coordination.agent_profiles["{AGENT_ID}"].role' in exc_info.value.fields


@pytest.mark.parametrize("agent_id", [None, "../private-agent", "agent-beta"])
def test_retired_hierarchy_precedes_identity_admission(agent_id: str | None) -> None:
    goal = {
        "coordination": {
            "agent_model": "peer_v1",
            "registered_agents": [AGENT_ID],
            "side_agent_handoff_agent": AGENT_ID,
        }
    }

    with pytest.raises(ValueError, match="retired v0.1 agent hierarchy"):
        build_quota_agent_identity(goal, agent_id=agent_id)


def test_profile_list_paths_use_source_indexes_and_redact_invalid_map_keys() -> None:
    list_goal = {
        "coordination": {
            "agent_model": "peer_v1",
            "agent_profiles": [
                {"agent_id": AGENT_ID, "schema_version": "agent_profile_v0"}
            ],
        }
    }
    with pytest.raises(ValueError) as list_error:
        build_quota_agent_identity(list_goal, agent_id=None)
    assert list_error.value.fields == (
        "coordination.agent_profiles[0].schema_version",
    )

    map_goal = {
        "coordination": {
            "agent_model": "peer_v1",
            "agent_profiles": {
                "synthetic.private/path": {"schema_version": "agent_profile_v0"}
            },
        }
    }
    with pytest.raises(ValueError) as map_error:
        build_quota_agent_identity(map_goal, agent_id=None)
    fingerprint = hashlib.sha256(b"synthetic.private/path").hexdigest()[:12]
    assert map_error.value.fields == (
        f'coordination.agent_profiles["<invalid-agent-id:{fingerprint}>"]'
        ".schema_version",
    )
    assert "synthetic.private/path" not in str(map_error.value)


def test_missing_agent_id_remains_blocked_for_current_agent_models() -> None:
    goal = {
        "coordination": {
            "agent_model": "peer_v1",
            "registered_agents": [AGENT_ID],
        }
    }

    assert build_quota_agent_identity(goal, agent_id=None) is None
    upgrade = build_identity_aware_prompt_upgrade(
        goal,
        goal_id=GOAL_ID,
        agent_identity=None,
    )

    assert upgrade is not None
    assert upgrade["blocks_should_run"] is True
    assert "registry_migration_required" not in upgrade
    assert "--agent-id" in upgrade["recommended_action"]


def test_quota_cli_reports_retired_hierarchy_fields_without_agent_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    runtime_root = tmp_path / "runtime"
    registry = project / ".loopx" / "registry.json"
    _write_state(project)
    write_fixture_registry(
        project=project,
        runtime_root=runtime_root,
        registry_path=registry,
        goal_id=GOAL_ID,
        domain="retired-hierarchy",
        adapter_kind="generic_project_goal_v0",
        registered_agents=[AGENT_ID],
        extra_goal_fields={
            "coordination": {
                "agent_model": "peer_v1",
                "registered_agents": [AGENT_ID],
                "side_agent_handoff_agent": AGENT_ID,
            }
        },
    )

    exit_code = main(
        [
            "--format",
            "json",
            "--registry",
            str(registry),
            "--runtime-root",
            str(runtime_root),
            "quota",
            "should-run",
            "--goal-id",
            GOAL_ID,
            "--scan-path",
            str(project),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["error_code"] == "retired_agent_hierarchy"
    assert payload["status"] == "retired_agent_hierarchy"
    assert payload["legacy_fields"] == [
        "coordination.side_agent_handoff_agent"
    ]
    assert "remove the listed fields" in payload["recommended_action"]
    assert "--agent-id" in payload["recommended_action"]
