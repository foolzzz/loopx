"""Scripted required-vision regression fixture, with no model or provider transport."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loopx.heartbeat_prompt import build_heartbeat_prompt
from loopx.paths import (
    ACTIVE_GOAL_STATE_FILENAME,
    GLOBAL_REGISTRY_FILENAME,
    PROJECT_GOAL_STATE_ROOT,
    home_runtime_root,
)
from loopx.control_plane.quota.effective_action import EffectiveAction
from vision_shell_host_support import VisionShellHost


def loopx_command_tokens(command: str) -> list[str] | None:
    """Return one bounded LoopX argv suffix, never a shell program."""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        if Path(token).name == "loopx":
            command_tokens = tokens[index:]
            if any(token in {";", "&&", "||", "|"} for token in command_tokens):
                return None
            return command_tokens
    return None


def argument_value(tokens: list[str], option: str) -> str | None:
    try:
        index = tokens.index(option)
    except ValueError:
        return None
    return tokens[index + 1] if index + 1 < len(tokens) else None


class LoopxCliExecutionError(RuntimeError):
    """An executed CLI returned nonzero; this does not imply state rollback."""

    def __init__(self, returncode: int, detail: str) -> None:
        super().__init__(f"LoopX CLI command failed with exit={returncode}: {detail}")
        self.returncode = returncode


@dataclass(frozen=True)
class VisionFixture:
    quota_guard_command: str
    project_root: Path
    runtime_root: Path
    global_registry_path: Path
    source_root: Path
    frontier_target: Path
    work_source_target: Path


def _build_fixture(root: Path) -> VisionFixture:
    project = root / "project"
    runtime = root / "runtime"
    source = project / "fixture/permission-config.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(
            {
                "schema_version": "permission_config_contract_v0",
                "default_role": "reader",
                "write_requires_explicit_grant": True,
                "public_safe": True,
            }
        ),
        encoding="utf-8",
    )
    frontier = project / "replan-frontier.json"
    frontier.write_text(
        json.dumps(
            {
                "uncovered": [
                    {
                        "source_ref": "fixture/permission-config.json",
                        "evidence_id": "evidence-permission-config",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    goal_id = "replan-semantic-action-fixture"
    agent_id = "codex-replan-semantic-action"
    relative = PROJECT_GOAL_STATE_ROOT / goal_id / ACTIVE_GOAL_STATE_FILENAME
    state = project / relative
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        '---\nstatus: active\nowner_mode: goal\nobjective: "Inspect the permission boundary."\n'
        "updated_at: 2026-08-13T00:00:00+08:00\n---\n\n# Vision Closeout Fixture\n\n"
        "## Objective\n\nInspect the permission boundary.\n\n## Next Action\n\n"
        "- Read `replan-frontier.json` and persist the typed result through refresh-state.\n\n"
        "## Agent Todo\n\n- [ ] [P1-monitor] Observe the fixture.\n"
        "  <!-- loopx:todo todo_id=todo-replan-semantic-action status=open "
        "task_class=continuous_monitor action_kind=observe_fixture "
        "claimed_by=codex-replan-semantic-action target_key=replan-semantic-fixture "
        "cadence=1d next_due_at=2999-01-01T00%3A00%3A00Z -->\n"
        "- [ ] [P0] Continue the peer-owned implementation.\n"
        "  <!-- loopx:todo todo_id=todo_peer_implementation status=open "
        "task_class=advancement_task claimed_by=codex-fixture-peer -->\n",
        encoding="utf-8",
    )
    registry = {
        "schema_version": "0.1",
        "updated_at": "2026-08-13T00:00:00+08:00",
        "common_runtime_root": str(runtime),
        "goals": [
            {
                "id": goal_id,
                "domain": "replan-semantic-action-fixture",
                "status": "active",
                "repo": str(project),
                "state_file": str(relative),
                "adapter": {
                    "kind": "fixture_connected_delivery_v0",
                    "status": "connected-delivery",
                },
                "coordination": {
                    "registered_agents": [agent_id, "codex-fixture-peer"],
                    "agent_model": "peer_v1",
                    "agent_profiles": {
                        agent_id: {
                            "schema_version": "agent_profile_v1",
                            "agent_id": agent_id,
                            "profile_role": "quality-qualification",
                            "scope_summary": "Validate vision closeout.",
                            "default_task_classes": [
                                "advancement_task",
                                "continuous_monitor",
                            ],
                            "vision_requirement": "required",
                        }
                    },
                },
                "authority_sources": [],
                "quota": {"compute": 1.0, "window_hours": 24, "allowed_slots": 5},
            }
        ],
    }
    global_registry = home_runtime_root(root / "home") / GLOBAL_REGISTRY_FILENAME
    for target in (project / ".loopx/registry.json", global_registry):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(registry), encoding="utf-8")
    runs = runtime / "goals" / goal_id / "runs"
    runs.mkdir(parents=True)
    (runs / "index.jsonl").write_text("", encoding="utf-8")
    prompt = build_heartbeat_prompt(
        goal_id=goal_id,
        thin=True,
        agent_id=agent_id,
        registered_agents=[agent_id],
        available_capabilities=["shell", "filesystem_read", "filesystem_write"],
        runtime_profile="generic_cli",
    )
    return VisionFixture(
        str(prompt["quota_guard_command"]),
        project,
        runtime,
        global_registry,
        Path(__file__).resolve().parents[2],
        frontier,
        source,
    )


@dataclass
class VisionState:
    fixture: VisionFixture
    turn_instance_id: str = "scripted-vision-closeout"
    quota_packet: dict[str, Any] | None = None
    work_source_read: bool = False
    semantic_delta: dict[str, Any] | None = None
    semantic_reentry_observation: dict[str, Any] | None = None
    vision_closeout: dict[str, Any] | None = None


@dataclass(frozen=True)
class ScriptedExecToolAction:
    command: str


@dataclass(frozen=True)
class ScriptedAssistantAction:
    content: str


class VisionHostAdmissionRejected(ValueError):
    """Reject the current operation before execution, not prior compound steps."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.detail = detail


def observe_shell_evidence(output: str, state: VisionState) -> None:
    """Observe returned source data, independent of the command used to read it."""
    expected = json.loads(state.fixture.work_source_target.read_text(encoding="utf-8"))
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if value == expected:
            state.work_source_read = True
            return


def _rows(state: VisionState) -> list[dict[str, Any]]:
    goal_id = str((state.quota_packet or {})["goal_id"])
    index = state.fixture.runtime_root / "goals" / goal_id / "runs" / "index.jsonl"
    if not index.is_file():
        return []
    return [
        json.loads(line)
        for line in index.read_text(encoding="utf-8").splitlines()
        if line
    ]


def dispatch_vision_closeout(
    command: str,
    state: VisionState,
    *,
    execute: Callable[..., str],
) -> tuple[str, str, bool] | None:
    tokens = loopx_command_tokens(command) or []
    if "refresh-state" not in tokens and "spend-slot" not in tokens:
        return None
    packet = state.quota_packet or {}
    binding = (
        dict(
            dict(
                dict(packet.get("interaction_contract") or {}).get("cli_channel") or {}
            ).get("replan_settlement_contract")
            or {}
        ).get("settlement_binding")
        or {}
    )
    if not binding or argument_value(tokens, binding["cli_argument"]) != binding["id"]:
        raise ValueError("vision_closeout_binding_mismatch")
    if argument_value(tokens, "--turn-instance-id") != state.turn_instance_id:
        raise ValueError("vision_closeout_turn_mismatch")
    if "refresh-state" in tokens:
        path = argument_value(tokens, "--agent-vision-json")
        if not path or not state.work_source_read:
            raise ValueError(
                "vision_closeout_requires_observed_source_and_authored_decision"
            )
        target = (state.fixture.project_root / path).resolve()
        if not target.is_relative_to(state.fixture.project_root.resolve()):
            raise ValueError("vision_authoring_path_outside_fixture")
        vision = json.loads(target.read_text(encoding="utf-8"))
        source_evidence = json.loads(
            state.fixture.frontier_target.read_text(encoding="utf-8")
        )["uncovered"]
        source_ref = state.fixture.work_source_target.relative_to(
            state.fixture.project_root
        ).as_posix()
        observed_refs = {
            ref
            for item in source_evidence
            if item.get("source_ref") == source_ref
            for ref in (item["evidence_id"], source_ref)
        }
        path_delta = vision.get("path_delta")
        refs = path_delta.get("evidence_refs") if isinstance(path_delta, dict) else None
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise ValueError("vision_closeout_evidence_not_observed")
        refs = set(refs)
        if not refs.intersection(observed_refs):
            raise VisionHostAdmissionRejected(
                "vision_closeout_evidence_not_observed",
                "No refresh was executed: path_delta.evidence_refs must identify the source evidence actually read. Accepted references: "
                + json.dumps(sorted(observed_refs)),
            )
        output = execute(
            command, fixture=state.fixture, turn_instance_id=state.turn_instance_id
        )
        rows = _rows(state)
        if not rows:
            raise VisionHostAdmissionRejected(
                "vision_closeout_durable_writeback_missing",
                "No run receipt was written; revise the vision decision and retry.",
            )
        row = rows[-1]
        semantic = (
            dict(row.get("autonomous_replan_ack") or {}).get("semantic_delta") or {}
        )
        checkpoint = dict(row.get("vision_checkpoint") or {})
        identity = dict(row.get("settlement_identity") or {})
        expected_identity = dict(packet.get("heartbeat_receipt") or {}).get(
            "settlement_identity"
        )
        if not (
            semantic.get("accepted") is True
            and semantic.get("obligation_id") == binding["id"]
            and checkpoint.get("satisfied") is True
            and identity == expected_identity
        ):
            raise ValueError("vision_closeout_durable_writeback_incomplete")
        state.semantic_delta = semantic
        state.vision_closeout = {
            "checkpoint_satisfied": True,
            "bound_writeback": True,
            "settled": False,
        }
        return output, "semantic_replan_writeback", False
    if state.vision_closeout is None:
        raise ValueError("vision_closeout_spend_before_writeback")
    output = execute(
        command, fixture=state.fixture, turn_instance_id=state.turn_instance_id
    )
    replay = json.loads(
        execute(
            state.fixture.quota_guard_command,
            fixture=state.fixture,
            turn_instance_id=state.turn_instance_id,
        )
    )
    following = json.loads(
        execute(
            state.fixture.quota_guard_command,
            fixture=state.fixture,
            turn_instance_id=f"{state.turn_instance_id}-readback",
        )
    )
    spends = [
        row for row in _rows(state) if row.get("classification") == "quota_slot_spent"
    ]
    if (
        replay.get("effective_action") != EffectiveAction.HEARTBEAT_SETTLED_SKIP.value
        or len(spends) != 1
    ):
        raise ValueError("vision_closeout_settlement_readback_failed")
    remaining = dict(following.get("autonomous_replan_obligation") or {})
    remaining_kinds = {item["kind"] for item in remaining.get("triggers", [])}
    if remaining.get("obligation_id") == binding["id"] or remaining_kinds.intersection(
        {"required_agent_vision_missing", "vision_checkpoint_missing"}
    ):
        raise ValueError("vision_closeout_rearmed_after_settlement")
    # A real successor requirement may follow a newly authored continuing
    # vision. Do not force the script to declare as_needed or no_followup just
    # to obtain a quiet next wake.
    state.vision_closeout.update(
        settled=True, spend_count=1, original_obligation_closed=True
    )
    state.semantic_reentry_observation = {
        "effective_action": following.get("effective_action"),
        "trigger_kinds": sorted(remaining_kinds),
    }
    return output, "quota_spend_slot", True


def _execute_loopx(
    command: str, *, fixture: VisionFixture, turn_instance_id: str
) -> str:
    tokens = loopx_command_tokens(command)
    if not tokens:
        raise ValueError("fixture requires one bounded LoopX command")
    if "--registry" not in tokens:
        tokens[1:1] = ["--registry", str(fixture.global_registry_path)]
    if "refresh-state" in tokens:
        tokens.extend(["--no-global-sync", "--suppress-external-sinks"])
    argv = tokens[1:]
    for option, value in {
        "--registry": str(fixture.global_registry_path),
        "--runtime-root": str(fixture.runtime_root),
        "--turn-instance-id": turn_instance_id,
    }.items():
        if option in argv:
            index = argv.index(option) + 1
            if index >= len(argv):
                raise ValueError(f"missing value for {option}")
            argv[index] = value
    if "quota" in argv and "--scan-root" not in argv and "--scan-path" not in argv:
        argv.extend(["--scan-root", str(fixture.project_root)])
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(fixture.source_root) + (
        os.pathsep + existing if existing else ""
    )
    completed = subprocess.run(
        [sys.executable, "-P", "-m", "loopx.cli", *argv],
        cwd=fixture.project_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if len(detail) > 1000:
            detail = detail[:500] + "\n...\n" + detail[-500:]
        raise LoopxCliExecutionError(completed.returncode, detail)
    return completed.stdout


def run_scripted_closeout(root: Path, actions: Sequence[Any]) -> dict[str, Any]:
    fixture = _build_fixture(root)
    state = VisionState(fixture)
    messages: list[dict[str, Any]] = []
    count = 0

    def invoke(argv: list[str], cwd: Path, output: str) -> str:
        observe_shell_evidence(output, state)
        tokens = ["loopx", *argv]
        if "--agent-vision-json" in tokens:
            index = tokens.index("--agent-vision-json") + 1
            if index < len(tokens):
                tokens[index] = str((cwd / tokens[index]).resolve())
        command = shlex.join(tokens)
        if "--help" in argv or "-h" in argv:
            return _execute_loopx(
                command, fixture=fixture, turn_instance_id=state.turn_instance_id
            )
        if "quota" in argv and "should-run" in argv:
            output = _execute_loopx(
                command, fixture=fixture, turn_instance_id=state.turn_instance_id
            )
            if state.quota_packet is None:
                state.quota_packet = json.loads(output)
                triggers = state.quota_packet["autonomous_replan_obligation"][
                    "triggers"
                ]
                assert "required_agent_vision_missing" in {
                    item["kind"] for item in triggers
                }
            return output
        result = dispatch_vision_closeout(command, state, execute=_execute_loopx)
        if result is None:
            raise ValueError(
                "This fixture permits quota, help, vision refresh and settlement only"
            )
        return result[0]

    host = VisionShellHost(
        fixture.project_root, invoke, turn_instance_id=state.turn_instance_id
    )
    try:
        for step in actions[:40]:
            action = step({"messages": messages}) if callable(step) else step
            if isinstance(action, ScriptedAssistantAction):
                break
            count += 1
            output, code = host.execute(action.command)
            observe_shell_evidence(output, state)
            messages.append(
                {
                    "role": "tool",
                    "content": json.dumps({"exit_code": code, "output": output})
                    if code
                    else output,
                }
            )
            if (state.vision_closeout or {}).get("settled"):
                break
    finally:
        host.close()
    return {
        "closeout_complete": bool((state.vision_closeout or {}).get("settled")),
        "execution_host": "os_isolated_shell",
        "boundary": {"shell_commands_executed": count > 0},
        "selected_semantic_outcomes": list(
            (state.semantic_delta or {}).get("satisfying_outcomes") or []
        ),
        "vision_closeout": state.vision_closeout,
        "semantic_reentry": state.semantic_reentry_observation,
        "tool_call_count": count,
        "tool_call_limit": 40,
    }
