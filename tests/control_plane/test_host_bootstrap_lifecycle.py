"""The persistent host entrypoint reloads policy, not scheduler ownership."""
import json
import shlex
import subprocess
import sys

import pytest


def cli(registry, *arguments):
    result = subprocess.run([sys.executable, "-m", "loopx.cli", "--format", "json",
        "--registry", str(registry), "heartbeat-prompt", "--goal-id", "fixture-goal",
        "--agent-id", "worker-a", *arguments], capture_output=True, text=True, timeout=60)
    return json.loads(result.stdout)


@pytest.fixture
def registry(tmp_path):
    state = tmp_path / "STATE.md"
    state.write_text("# Fixture\n")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"goals": [{"id": "fixture-goal", "repo": str(tmp_path),
        "state_file": str(state), "registered_agents": ["worker-a"]}]}))
    return registry


@pytest.mark.parametrize("flags", [
    ["--runtime-profile", "codex_cli"],
    ["--runtime-profile", "ark_managed_agent_goal"],
    ["--runtime-profile", "generic_cli", "--visible-goal-host", "traex-cli"],
])
def test_bootstrap_real_cli_load_is_one_level_and_retains_host(registry, flags):
    initial = cli(registry, "--bootstrap", *flags)
    assert initial["ok"] and initial["bootstrap"]
    assert initial["task_body"].startswith("LoopX managed host bootstrap v1\n")
    assert "不创建新 Goal、不接管宿主调度" in initial["task_body"]
    assert "refresh-state" not in initial["task_body"]
    command = shlex.split(initial["task_body"].split("```sh\n")[1].split("\n```", 1)[0])
    assert "--bootstrap" not in command
    loaded = subprocess.run([sys.executable, "-m", "loopx.cli", *command[1:]],
        capture_output=True, text=True, timeout=60, check=True)
    body = json.loads(loaded.stdout)
    direct = cli(registry, *flags)
    assert body["task_body"] == direct["task_body"]
    assert body["runtime_profile"] == direct["runtime_profile"]
    assert body.get("bootstrap") is not True
    assert "interaction_contract" in body["task_body"]


def test_bootstrap_preserves_explicit_policy_and_does_not_freeze_registry_scope(registry):
    policy = "Only change the assigned files; don't expand scope."
    packet = cli(registry, "--bootstrap", "--runtime-profile", "codex_cli",
                 "--permission-rule", policy)
    command = shlex.split(packet["task_body"].split("```sh\n")[1].split("\n```", 1)[0])
    assert command[command.index("--permission-rule") + 1] == policy
    assert "--active-state" not in command
    assert "--agent-scope" not in command
    assert packet["interface_budget"]["char_count"] == len(packet["task_body"])


def test_saved_goal_bootstrap_reloads_changed_state_and_rejects_removed_agent(registry, tmp_path):
    packet = cli(registry, "--bootstrap", "--runtime-profile", "codex_cli")
    command = shlex.split(packet["task_body"].split("```sh\n")[1].split("\n```", 1)[0])
    saved = json.loads(registry.read_text())
    replacement = tmp_path / "NEW_STATE.md"
    replacement.write_text("# New current state\n")
    saved["goals"][0]["state_file"] = str(replacement)
    registry.write_text(json.dumps(saved))
    def load():
        result = subprocess.run([sys.executable, "-m", "loopx.cli", *command[1:]],
            capture_output=True, text=True, timeout=60)
        return json.loads(result.stdout)
    assert load()["resolved_active_state"] == str(replacement)
    saved["goals"][0]["registered_agents"] = ["worker-b"]
    registry.write_text(json.dumps(saved))
    rejected = load()
    assert rejected["ok"] is False
    assert not rejected.get("task_body")


@pytest.mark.parametrize("with_agent_id", [False, True])
@pytest.mark.parametrize(
    ("legacy_coordination", "expected_field"),
    [
        (
            {"agent_profiles": {"worker-a": {"schema_version": "agent_profile_v0"}}},
            'coordination.agent_profiles["worker-a"].schema_version',
        ),
        (
            {"agent_profiles": {"worker-a": {"review_policy": {"can_self_merge": True}}}},
            'coordination.agent_profiles["worker-a"].review_policy.can_self_merge',
        ),
        (
            {"completed_migrations": {"peer_agent_runtime_v1": {"status": "completed"}}},
            "coordination.completed_migrations.peer_agent_runtime_v1",
        ),
    ],
)
def test_heartbeat_prompt_rejects_retired_hierarchy_before_profile_projection(
    registry, legacy_coordination, expected_field, with_agent_id
):
    saved = json.loads(registry.read_text())
    saved["goals"][0]["coordination"] = {
        "registered_agents": ["worker-a"],
        **legacy_coordination,
    }
    registry.write_text(json.dumps(saved))
    arguments = [
        sys.executable,
        "-m",
        "loopx.cli",
        "--format",
        "json",
        "--registry",
        str(registry),
        "heartbeat-prompt",
        "--goal-id",
        "fixture-goal",
    ]
    if with_agent_id:
        arguments.extend(["--agent-id", "worker-a"])

    result = subprocess.run(arguments, capture_output=True, text=True, timeout=60)
    payload = json.loads(result.stdout)

    assert result.returncode != 0
    assert payload["ok"] is False
    assert expected_field in payload["error"]
    assert "retired v0.1 agent hierarchy" in payload["error"]


def test_bootstrap_rejects_persisted_turn_and_invalid_binding(registry):
    assert not cli(
        registry,
        "--bootstrap",
        "--runtime-profile",
        "generic_cli",
        "--turn-instance-id",
        "fixed-turn",
    )["ok"]


def test_generic_brief_with_registry_profile_keeps_budget_and_current_settlement(registry):
    scopes = [
        "Maintain shared runtime contracts and validate compatibility across hosts. "
        "Use isolated worktrees and exercise public entrypoints.",
        "Record evidence, leave unrelated work untouched and coordinate peer-owned "
        "tasks through their owners.",
    ]
    saved = json.loads(registry.read_text())
    saved["goals"][0]["coordination"] = {
        "registered_agents": ["worker-a"], "agent_model": "peer_v1",
        "agent_profiles": {"worker-a": {"schema_version": "agent_profile_v1", "scopes": scopes}},
    }
    registry.write_text(json.dumps(saved))
    packet = cli(registry, "--brief", "--runtime-profile", "generic_cli")
    assert packet["ok"], packet.get("error")
    body = packet["task_body"]
    assert all(scope.rstrip(".!?") in body for scope in scopes)
    assert packet["agent_scope_source"] == "agent_profile_v1"
    assert packet["interface_budget"]["max_chars"] == 4300
    assert packet["interface_budget"]["within_budget"], packet["interface_budget"]
    assert packet["cli_preflight"] in body
    assert "--runtime-profile generic_cli" in body
    assert "execution_obligation.must_attempt_work" in body
    assert "heartbeat_recommendation.agent_must_attempt" in body
    assert "interaction_contract.cli_channel.settlement_plan.ordered_steps" in body
    assert "terminal no-follow-up" in body
    assert packet["quota_spend_command"] not in body
    assert packet["progress_refresh_state_command"] not in body
    assert packet["refresh_state_command"] not in body
