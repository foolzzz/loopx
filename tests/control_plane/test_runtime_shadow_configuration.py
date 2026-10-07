from __future__ import annotations

import json
from pathlib import Path

import pytest

from loopx.cli import main
from loopx.configure_goal import configure_goal


GOAL_ID = "runtime-shadow-config"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _registry(tmp_path: Path) -> Path:
    state = tmp_path / "ACTIVE_GOAL_STATE.md"
    state.write_text(
        "---\n"
        f"goal_id: {GOAL_ID}\n"
        "handoff_mode: hard_lease\n"
        "---\n\n"
        "## Agent Todo\n\n",
        encoding="utf-8",
    )
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "goals": [
                    {
                        "id": GOAL_ID,
                        "repo": str(tmp_path),
                        "state_file": state.name,
                        "coordination": {
                            "agent_model": "peer_v1",
                            "registered_agents": ["agent-a", "agent-b"],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return registry


def test_configure_goal_exposes_transaction_bound_runtime_shadow_separately(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)

    preview = configure_goal(
        registry_path=registry,
        goal_id=GOAL_ID,
        coordination_runtime_shadow_file=True,
        execute=False,
    )
    assert preview["changed_fields"] == ["coordination_runtime_shadow"]
    assert preview["before"]["coordination_runtime_shadow"] == {
        "enabled": False,
        "provider": None,
        "status": "configuration_absent",
    }
    assert preview["after"]["coordination_runtime_shadow"] == {
        "enabled": True,
        "provider": "file_v0",
        "status": "enabled",
    }

    applied = configure_goal(
        registry_path=registry,
        goal_id=GOAL_ID,
        coordination_runtime_shadow_file=True,
        execute=True,
    )
    assert applied["written"] is True
    goal = json.loads(registry.read_text(encoding="utf-8"))["goals"][0]
    assert goal["coordination"]["runtime_shadow"] == {
        "enabled": True,
        "schema_version": "loopx_coordination_runtime_shadow_config_v0",
        "provider": "file_v0",
    }
    assert "authority_shadow" not in goal["coordination"]

    feature = next(
        item
        for item in applied["configuration_catalog"]["features"]
        if item["feature_id"] == "coordination_runtime_shadow"
    )
    assert feature["current"]["enabled"] is True
    assert feature["commands"]["apply_enable"].endswith(
        "--coordination-runtime-shadow-file --execute"
    )

    cleared = configure_goal(
        registry_path=registry,
        goal_id=GOAL_ID,
        clear_coordination_runtime_shadow=True,
        execute=True,
    )
    assert cleared["changed_fields"] == ["coordination_runtime_shadow"]
    goal = json.loads(registry.read_text(encoding="utf-8"))["goals"][0]
    assert "runtime_shadow" not in goal["coordination"]



@pytest.mark.parametrize("arguments", [
    ["authority-shadow", "status", "--goal-id", GOAL_ID],
    ["configure-goal", "--goal-id", GOAL_ID, "--local-authority-shadow-file"],
    ["configure-goal", "--goal-id", GOAL_ID, "--clear-local-authority-shadow"],
])
def test_removed_observation_cli_rejects_before_registry_write(tmp_path: Path, arguments: list[str]) -> None:
    registry = _registry(tmp_path)
    before = registry.read_bytes()
    with pytest.raises(SystemExit) as exc:
        main(["--registry", str(registry), *arguments])
    assert exc.value.code == 2
    assert registry.read_bytes() == before


def test_configuration_catalog_omits_observation_and_runtime_stays_default_off(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    result = configure_goal(registry_path=registry, goal_id=GOAL_ID)
    assert "local_authority_shadow" not in result["feature_summary"]
    assert "local_authority_shadow" not in result["after"]
    features = {row["feature_id"] for row in result["configuration_catalog"]["features"]}
    assert "local_authority_shadow" not in features
    assert "coordination_runtime_shadow" in features
    assert result["after"]["coordination_runtime_shadow"]["enabled"] is False
