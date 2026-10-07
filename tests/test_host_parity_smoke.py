"""Thin pytest: host parity — agent types, scheduler bindings, Turn hosts,
host mode plan routing, fail-closed capability routes, no-spend transition,
public safety.

Covers GH-C60 core assertions.
"""

from __future__ import annotations

import json
import re

import pytest

from loopx.host_loop_activation import (
    HOST_SURFACE_TO_AGENT_TYPE,
    SUPPORTED_AGENT_TYPES,
    agent_type_for_host_surface,
    agent_type_uses_host_managed_skills,
    build_agent_type_catalog,
    build_host_loop_activation_packet,
    normalize_agent_type,
    scheduler_command_binding_for_agent_type,
)
from loopx.host_mode_planner import (
    CANONICAL_MODES,
    MODE_HYBRID_HANDOFF,
    MODE_IM_GATEWAY,
    MODE_ISOLATED_HEADLESS_TURN,
    MODE_SHELL_SERVICE,
    MODE_VISIBLE_TUI,
    SUPPORTED_TURN_HOST_IDENTITIES,
    VISIBLE_HOST_CONNECTOR_IDS,
    VISIBLE_PI_ALIASES,
    HostModePlanError,
    build_host_mode_plan,
)
from loopx.control_plane.turn_driver.driver import (
    SUPPORTED_HOSTS,
    SUPPORTED_EXECUTION_MODES,
)

_PRIVATE_RE = [
    re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    re.compile(r"/home/[A-Za-z0-9._-]+/"),
    re.compile(r"\bBearer" + r"\s+[A-Za-z0-9._-]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
]


def _public_safe(obj: object) -> bool:
    text = json.dumps(obj, sort_keys=True) if not isinstance(obj, str) else obj
    return not any(p.search(text) for p in _PRIVATE_RE)


# -- Agent type catalog -------------------------------------------------------

_FIXTURE_GOAL = "parity-test"
_FIXTURE_AGENT = "parity-agent"


class TestAgentTypeCatalog:
    def test_canonical_count(self):
        catalog = build_agent_type_catalog()
        assert catalog["ok"]
        types = {t["agent_type"] for t in catalog["canonical_agent_types"]}
        assert "codex-cli" in types
        assert "claude-code" in types
        assert "pi" in types
        assert "cursor-agent" in types
        assert "ark-managed-agent" in types
        assert "other-agent" in types
        assert "manual" in types
        assert types == {
            "ark-managed-agent", "codex-cli", "claude-code", "traex-cli", "pi",
            "cursor-agent", "zcode", "agy", "deepseek-harness",
            "deepseek-harness-native", "manual", "other-agent",
        }

        ambiguous = {item["input"]: item["use_one_of"]
                     for item in catalog["ambiguous_inputs"]}
        assert ambiguous == {"cli": ["codex-cli", "manual", "other-agent"]}

    @pytest.mark.parametrize("surface,expected", [
        ("codex-cli-tui", "codex-cli"),
        ("claude-code", "claude-code"),
        ("traex", "traex-cli"),
        ("pi", "pi"),
        ("cursor", "cursor-agent"),
        ("shell", "manual"),
        ("http", "other-agent"),
        ("worker-bridge", "other-agent"),
    ])
    def test_host_surface_mapping(self, surface, expected):
        assert agent_type_for_host_surface(surface) == expected
        assert HOST_SURFACE_TO_AGENT_TYPE[surface] == expected

    def test_normalize_agent_type(self):
        assert normalize_agent_type("codex") == "codex-cli"
        assert normalize_agent_type("pi") == "pi"
        assert normalize_agent_type("traex") == "traex-cli"

    def test_host_managed_skill_types(self):
        host = {
            "ark-managed-agent",
            "deepseek-harness-native",
            "traex-cli",
            "other-agent",
        }
        for at in SUPPORTED_AGENT_TYPES:
            canonical = normalize_agent_type(at)
            assert agent_type_uses_host_managed_skills(canonical) == (
                canonical in host)


# -- Scheduler bindings -------------------------------------------------------

class TestSchedulerBindings:
    def test_manual_and_other_agent_have_no_profile(self):
        assert scheduler_command_binding_for_agent_type("manual") == {}
        assert scheduler_command_binding_for_agent_type("other-agent") == {}


# -- Turn host identities -----------------------------------------------------

class TestTurnHostIdentities:
    def test_supported_hosts(self):
        assert SUPPORTED_HOSTS == {"codex-cli", "claude-code", "dsh", "generic-cli"}
        assert set(SUPPORTED_TURN_HOST_IDENTITIES) == {
            "codex-cli",
            "claude-code",
            "generic-cli",
            "pi",
        }
        assert "dsh" not in SUPPORTED_TURN_HOST_IDENTITIES

    def test_execution_modes(self):
        assert SUPPORTED_EXECUTION_MODES == {"interactive-visible",
                                              "isolated-headless"}

    def test_visible_connectors(self):
        assert VISIBLE_HOST_CONNECTOR_IDS == {
            "codex-cli": "codex_cli_tui",
            "claude-code": "claude_code_loop",
            "pi": "pi_goal_loop",
        }

    def test_pi_visible_identity(self):
        assert VISIBLE_PI_ALIASES == {"pi": "generic-cli"}


# -- Host mode plan routing ---------------------------------------------------

def _plan(intent, host_identity="codex-cli"):
    return build_host_mode_plan(
        goal_id="p", user_intent=intent,
        host_capabilities=["visible_session", "loopx_turn",
                           "typed_host_adapter", "independent_validator",
                           "chat_gateway", "service_timer", "shell"],
        agent_id="a", registered_agents=["a"],
        host_identity=host_identity)


class TestHostModePlanRouting:
    def test_intent_to_mode(self):
        expect = {
            "watch_each_turn": MODE_VISIBLE_TUI,
            "continue_without_ui": MODE_ISOLATED_HEADLESS_TURN,
            "intake_from_chat": MODE_IM_GATEWAY,
            "timer_keepalive": MODE_SHELL_SERVICE,
            "escalate_between_modes": MODE_HYBRID_HANDOFF,
        }
        for intent, mode in expect.items():
            p = _plan(intent)
            assert p["ok"]
            assert p["selected_mode"] == mode
            assert {m["mode"] for m in p["mode_options"]} == set(CANONICAL_MODES)

    @pytest.mark.parametrize("host_id,connector", [
        ("codex-cli", "codex_cli_tui"),
        ("claude-code", "claude_code_loop"),
    ])
    def test_visible_connector_per_host(self, host_id, connector):
        p = build_host_mode_plan(
            goal_id="p", user_intent="watch_each_turn",
            host_capabilities=["visible_session"],
            agent_id="a", registered_agents=["a"],
            host_identity=host_id)
        assert p["selected_connector_id"] == connector
        assert p["selected_turn_mapping"]["host"] == host_id

    def test_pi_alias_routing(self):
        p = _plan("watch_each_turn", host_identity="pi")
        assert p["host_identity"] == "generic-cli"
        assert p["selected_connector_id"] == "pi_goal_loop"
        assert p["selected_turn_mapping"]["host"] == "generic-cli"

    def test_generic_visible_identity_does_not_claim_a_host(self):
        p = _plan("watch_each_turn", host_identity="generic-cli")
        assert p["selected_capability_ready"] is False
        assert p["selected_connector_id"] is None
        assert p["selected_turn_mapping"]["host"] is None

    def test_declared_host_capabilities_make_selected_mode_ready(self):
        visible = build_host_mode_plan(
            goal_id="g", user_intent="watch_each_turn",
            host_capabilities=["visible_session"],
            agent_id="a", registered_agents=["a"],
            available_capabilities=["shell", "network"],
            host_identity="codex-cli")
        headless = build_host_mode_plan(
            goal_id="g", user_intent="continue_without_ui",
            host_capabilities=["loopx_turn", "typed_host_adapter", "independent_validator"],
            agent_id="a", registered_agents=["a"],
            available_capabilities=["shell"], host_identity="generic-cli")
        assert visible["selected_capability_ready"] is True
        assert headless["selected_mode"] == "isolated_headless_turn"
        assert headless["selected_capability_ready"] is True


# -- Fail-closed --------------------------------------------------------------

class TestFailClosed:
    def test_visible_without_identity_blocked(self):
        p = build_host_mode_plan(
            goal_id="p", user_intent="watch_each_turn",
            host_capabilities=["visible_session"],
            registered_agents=["a"])
        assert p["selected_capability_ready"] is False
        assert p["selected_connector_id"] is None

    def test_headless_missing_adapter_and_validator(self):
        p = build_host_mode_plan(
            goal_id="p", user_intent="continue_without_ui",
            host_capabilities=["loopx_turn"],
            agent_id="a", registered_agents=["a"])
        missing = p["selected_missing_host_capabilities"]
        assert "typed_host_adapter" in missing
        assert "independent_validator" in missing
        assert p["selected_capability_ready"] is False

    def test_unknown_intent_raises_error(self):
        with pytest.raises(HostModePlanError) as exc:
            build_host_mode_plan(goal_id="p", user_intent="launch_missiles")
        assert exc.value.to_payload()["field"] == "user_intent"

    def test_unknown_host_identity_raises_error(self):
        with pytest.raises(HostModePlanError) as exc:
            build_host_mode_plan(
                goal_id="p", user_intent="watch_each_turn",
                host_capabilities=["visible_session"],
                host_identity="not-a-host",
                registered_agents=["a"])
        assert exc.value.to_payload()["field"] == "host_identity"

    def test_unknown_capability_raises_error(self):
        with pytest.raises(HostModePlanError) as exc:
            build_host_mode_plan(
                goal_id="p", user_intent="continue_without_ui",
                host_capabilities=["root_shell"])
        assert exc.value.to_payload()["field"] == "host_capabilities"


# -- No-spend transition (selector stays preview-only) ------------------------

class TestNoSpendTransition:
    """Host-mode selector must never spend; spend only follows validated writeback.

    GH-C60 missing slice: protocol host-mode-plan-v0 acceptance items 5–6
    (no-spend policy + private/authority boundary) were only covered by the
    example smoke, not by the thin host-parity pytest.
    """

    def test_no_spend_policy_covers_preview_and_quiet_paths(self):
        p = build_host_mode_plan(
            goal_id="p",
            user_intent="watch_each_turn",
            host_capabilities=["visible_session"],
            registered_agents=["a", "b"],
        )
        no_spend = p["no_spend_policy"]
        assert no_spend == {
            "selector_preview": True,
            "turn_plan_preview": True,
            "quiet_monitor_skip": True,
            "cadence_only_change": True,
            "readiness_or_final_check": True,
            "spends_only_after_validated_delivery_writeback": True,
        }
        assert _public_safe(no_spend)

    def test_boundary_keeps_selector_non_authoritative(self):
        p = build_host_mode_plan(
            goal_id="p",
            user_intent="watch_each_turn",
            host_capabilities=["visible_session"],
            registered_agents=["a", "b"],
        )
        boundary = p["boundary"]
        assert boundary["selector_is_authoritative"] is False
        assert boundary["turn_envelope_is_authoritative_for_execution"] is True
        assert boundary["adapter_neutral"] is True
        for key in (
            "starts_process",
            "writes_state",
            "spends_quota",
            "infers_production_permission",
            "infers_credential_access",
            "infers_destructive_authority",
        ):
            assert boundary[key] is False, (key, boundary)
        assert _public_safe(boundary)

    def test_operator_next_steps_are_marked_no_spend(self):
        p = build_host_mode_plan(
            goal_id="p",
            user_intent="continue_without_ui",
            host_capabilities=["loopx_turn"],
            agent_id="a",
            registered_agents=["a"],
        )
        steps = p["operator_next_steps"]
        assert steps
        assert all(step.get("no_spend") is True for step in steps), steps
        assert steps[0]["kind"] == "stop"
        assert _public_safe(steps)


# -- Public safety ------------------------------------------------------------

class TestPublicSafety:
    def test_activation_packets_public_safe(self):
        for at in SUPPORTED_AGENT_TYPES:
            canonical = normalize_agent_type(at)
            pkt = build_host_loop_activation_packet(
                agent_type=canonical, goal_id=_FIXTURE_GOAL,
                agent_id=_FIXTURE_AGENT,
                registered_agents=[_FIXTURE_AGENT])
            assert _public_safe(pkt), f"{canonical} leaked private data"

    def test_agent_type_catalog_public_safe(self):
        assert _public_safe(build_agent_type_catalog())

    def test_identity_gating(self):
        pkt = build_host_loop_activation_packet(
            agent_type="codex-cli", goal_id=_FIXTURE_GOAL,
            agent_id=None, registered_agents=["alpha", "beta"])
        assert not pkt["activation_allowed"]
        assert pkt["activation_state"] == "selection_required"
        gate = pkt["identity_selection_gate"]
        assert gate is not None
        assert len(gate["choices"]) == 2
        for c in gate["choices"]:
            assert c["mode"] == "takeover_existing_agent"

        pkt2 = build_host_loop_activation_packet(
            agent_type="codex-cli", goal_id=_FIXTURE_GOAL,
            agent_id=None, registered_agents=["solo"])
        assert pkt2["activation_allowed"]
        assert pkt2["activation_state"] == "single_registered_agent_selected"
        assert pkt2["agent_id"] == "solo"


@pytest.mark.parametrize("argv", [
    ["opencode2-goal-worker", "--help"],
    ["slash-commands", "--with-goal-bridge"],
    ["slash-commands", "--opencode-home", "fixture"],
    ["slash-commands", "--gemini-home", "fixture"],
    ["chat", "--kiro-cli-bin", "fixture"],
    ["dashboard", "--kiro-cli-bin", "fixture"],
    ["host-mode-plan", "--goal-id", "fixture", "--intent", "watch_each_turn",
     "--host-identity", "opencode"],
])
def test_retired_host_cli_routes_are_rejected(argv):
    from loopx.cli import build_parser

    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 2


def test_retained_chat_runtime_selectors_still_parse():
    from loopx.cli import build_parser

    for command in ("chat", "dashboard"):
        args = build_parser().parse_args([
            command, "--codex-bin", "fixture-codex", "--claude-bin", "fixture-claude",
        ])
        assert args.codex_bin == "fixture-codex"
        assert args.claude_bin == "fixture-claude"


def test_builtin_chat_catalog_keeps_retained_hosts(tmp_path, monkeypatch):
    from loopx.chat_endpoint_catalog import builtin_chat_endpoints

    monkeypatch.setenv("HOME", str(tmp_path))
    rows = builtin_chat_endpoints(
        codex_bin="fixture-missing-codex", claude_bin="fixture-missing-claude",
        runtime_root=tmp_path / "runtime",
    )
    ids = {row["agent_id"] for row in rows}
    assert {"codex", "claude-code", "dsh"} <= ids
    assert not ids & {"opencode", "opencode2", "kiro-cli", "gemini-cli", "cline"}
