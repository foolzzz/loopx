#!/usr/bin/env python3
"""Smoke-test catalog-informed canary profile planning.

Selection is checked as a table from (changed files, surfaces) to the exact
domain and catalog-family profile ids the plan selects. A second table pins the
scripts each profile must keep scheduling, by tier. Invariants hold for every profile: each
planned command names a script that exists, and deep checks appear only on
explicit request.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
CATALOG = REPO_ROOT / "docs" / "concepts" / "interaction-pattern-catalog.md"

from loopx.canary.planner import (  # noqa: E402
    build_catalog_canary_coverage_audit,
    build_catalog_canary_plan,
    build_catalog_canary_profiles,
)
from loopx.cli_commands.canary import collect_git_diff_changed_files  # noqa: E402

SCRIPT_TOKEN = re.compile(r"^[\w./-]+\.(?:py|mjs|js|ts|sh)$")

# (label, plan inputs, the exact domain profile ids, the exact catalog family profile ids)
SELECTION_CASES: tuple[tuple[str, dict[str, object], tuple[str, ...], tuple[str, ...]], ...] = (
    ("pr review", {"changed_files": ["loopx/pr_review.py", "skills/loopx-pr-review/SKILL.md"],
                   "surfaces": ["pr-review public PR metadata"]},
     ("pr-review-and-merge", "repo-architecture-budget"),
     ("state-and-boundary",)),
    ("release", {"changed_files": ["docs/product/release-readiness.md"],
                 "surfaces": ["release promotion install update"]},
     ("install-update", "release-promotion", "state-write-correctness"),
     ("state-and-boundary",)),
    ("install", {"changed_files": ["scripts/install-local.sh", "loopx/self_update.py"],
                 "surfaces": ["install update rollback"]},
     ("install-update", "repo-architecture-budget"),
     ("state-and-boundary",)),
    ("install without release", {"changed_files": ["loopx/doctor.py", "examples/install-local-smoke.py"]},
     ("install-update", "repo-architecture-budget"),
     ()),
    ("refactor", {"changed_files": ["loopx/quota.py", "loopx/status.py"],
                  "surfaces": ["control-plane refactor scheduler hint"]},
     ("agent-facing-cli-output-budget", "control-plane-refactor", "monitor-scheduler", "repo-architecture-budget", "status-read-path"),
     ("planning-governance", "state-and-boundary", "work-routing")),
    ("scheduler cadence", {"changed_files": ["loopx/control_plane/scheduler/monitor_schedule.py"],
                           "surfaces": ["scheduler cadence backoff reset"]},
     ("monitor-scheduler", "repo-architecture-budget", "scheduler-cadence"),
     ("planning-governance", "work-routing")),
    ("state machine", {"changed_files": ["examples/control_plane/control-plane-integrated-canary-smoke.py"],
                       "surfaces": ["complex control-plane state-machine interaction_contract "
                                    "scheduler_hint work_lane_contract goal_frontier"]},
     ("auto-research-demo", "control-plane-refactor", "control-plane-state-machine", "monitor-scheduler", "repo-architecture-budget", "runtime-connector-catalog", "scheduler-cadence"),
     ("work-routing",)),
    ("frontier rules", {"changed_files": ["loopx/control_plane/goals/goal_frontier/replan_rules.py"],
                        "surfaces": ["ordered goal frontier replan policy"]},
     ("auto-research-demo", "control-plane-state-machine", "goal-frontier-replan-rules", "repo-architecture-budget"),
     ("planning-governance", "state-and-boundary")),
    ("interaction contract", {"changed_files": ["loopx/control_plane/work_items/interaction_contract.py"],
                              "surfaces": ["interaction_contract protocol_action_packet state-machine"]},
     ("control-plane-refactor", "control-plane-state-machine", "repo-architecture-budget"),
     ("work-routing",)),
    ("interaction smoke", {"changed_files": ["examples/control_plane/interaction-contract-state-machine-smoke.py"]},
     ("control-plane-state-machine", "repo-architecture-budget"),
     ()),
    ("bounded context", {"changed_files": ["loopx/control_plane/work_items/work_lane.py"],
                         "surfaces": ["bounded-context work_lane_contract state-machine interaction_contract"]},
     ("control-plane-refactor", "control-plane-state-machine", "repo-architecture-budget"),
     ("work-routing",)),
    ("work-lane policy", {"changed_files": ["loopx/control_plane/scheduler/monitor_todo.py"],
                          "surfaces": ["resume_when resume_ready work-lane policy seam"]},
     ("control-plane-refactor", "monitor-scheduler", "repo-architecture-budget"),
     ("state-and-boundary", "work-routing")),
    ("monitor target", {"changed_files": ["loopx/control_plane/quota/monitor_poll_commit.ts"],
                        "surfaces": ["monitor_target monitor-poll scheduler_hint state-machine"]},
     ("control-plane-refactor", "control-plane-state-machine", "monitor-scheduler", "repo-architecture-budget", "runtime-connector-catalog", "scheduler-cadence"),
     ("planning-governance", "work-routing")),
    ("monitor writeback", {"changed_files": ["loopx/control_plane/scheduler/monitor_poll_writeback.py"],
                           "surfaces": ["monitor_poll_writeback scheduler_hint state-machine"]},
     ("control-plane-refactor", "control-plane-state-machine", "monitor-scheduler", "repo-architecture-budget", "runtime-connector-catalog", "scheduler-cadence"),
     ("work-routing",)),
    ("status", {"changed_files": ["loopx/status.py"], "surfaces": ["status --goal-id read-path"]},
     ("agent-facing-cli-output-budget", "control-plane-refactor", "repo-architecture-budget", "status-read-path"),
     ("state-and-boundary", "work-routing")),
    ("status cache", {"changed_files": ["loopx/control_plane/runtime/status_projection_cache.py"],
                      "surfaces": ["status_projection_cache projection-cache read-path"]},
     ("repo-architecture-budget", "status-projection-cache", "status-read-path"),
     ("state-and-boundary", "work-routing")),
    ("runtime handoff", {"changed_files": ["loopx/control_plane/handoff/project_handoff.py"],
                         "surfaces": ["runtime handoff post_handoff_run status read-path"]},
     ("control-plane-refactor", "repo-architecture-budget", "status-read-path"),
     ("state-and-boundary", "work-routing")),
    ("review packet", {"changed_files": ["loopx/review_packet.py", "loopx/cli_commands/status.py"],
                       "surfaces": ["review-packet handoff-only operator packet read-path"]},
     ("agent-facing-cli-output-budget", "cli-command-contract", "control-plane-refactor", "repo-architecture-budget", "review-packet-read-path", "status-read-path"),
     ("human-decision", "work-routing")),
    ("event read", {"changed_files": ["loopx/event_sourced_state.py", "loopx/rollout_event_log.py"],
                    "surfaces": ["event projection downstream read event-store read-path"]},
     ("event-sourced-read-path", "frontstage-rollout", "repo-architecture-budget", "status-read-path"),
     ("evidence-lifecycle", "state-and-boundary")),
    ("cli", {"changed_files": ["loopx/cli.py", "loopx/cli_commands/version.py"],
             "surfaces": ["cli command modularization"]},
     ("agent-facing-cli-output-budget", "cli-command-contract", "repo-architecture-budget"),
     ()),
    ("cli output budget", {"changed_files": ["loopx/cli_commands/status.py", "loopx/help_surface.py"],
                           "surfaces": ["agent-facing CLI output qualification"]},
     ("agent-facing-cli-output-budget", "cli-command-contract", "control-plane-refactor", "repo-architecture-budget", "review-packet-read-path", "status-read-path"),
     ("evidence-lifecycle", "work-routing")),
    ("bootstrap packet summary", {"changed_files": ["loopx/bootstrap_packet_summary.py"]},
     ("agent-facing-cli-output-budget", "repo-architecture-budget"),
     ()),
    ("todo", {"changed_files": ["loopx/todos.py", "loopx/control_plane/todos/contract.py"],
              "surfaces": ["todo lifecycle todo claim todo list"]},
     ("agent-facing-cli-output-budget", "repo-architecture-budget", "todo-lifecycle"),
     ("evidence-lifecycle", "human-decision", "planning-governance", "state-and-boundary", "work-routing")),
    ("product entry", {"changed_files": ["README.md", "loopx/capabilities/issue_fix/README.md",
                                         "docs/update-notes/README.md",
                                         "loopx/capabilities/content_ops/surface.py",
                                         "scripts/update_notes_release_job.py"],
                       "surfaces": ["product-entry issue-fix content-ops update-note cross-runtime demo"]},
     ("active-host-doc-guidance", "cross-runtime-impl-review-demo", "issue-fix-reviewer-routing", "product-entry-workflows", "release-promotion", "repo-architecture-budget", "state-write-correctness"),
     ("evidence-lifecycle",)),
    ("Chinese product entry", {"changed_files": ["README.zh-CN.md"]},
     ("active-host-doc-guidance",),
     ()),
    ("Dev Book host guidance", {"changed_files": ["docs/book/en/index.md"]},
     ("active-host-doc-guidance",),
     ()),
    ("unrelated nested Dev Book text", {"changed_files": ["other/docs/book/en/index.md"]},
     (),
     ()),
    ("Dev Book backup", {"changed_files": ["docs/book/en/index.md.bak"]},
     (),
     ()),
    ("profile id in unrelated filename", {"changed_files": ["other/active-host-doc-guidance.txt"]},
     (),
     ()),
    ("explicit host guidance surface", {"surfaces": ["active-host-doc-guidance"]},
     ("active-host-doc-guidance",),
     ()),
    ("host guard implementation", {"changed_files": ["examples/support/active_host_doc_guard.py"]},
     ("active-host-doc-guidance", "repo-architecture-budget"),
     ()),
    ("retired heartbeat prompt", {"changed_files": ["docs/heartbeat-automation-prompt.md"]},
     ("active-host-doc-guidance",),
     ("work-routing",)),
    ("state interaction host", {"changed_files": ["docs/state-interaction-model.md"]},
     ("active-host-doc-guidance",),
     ()),
    ("Codex CLI automation driver", {"changed_files": ["docs/product/runtimes/codex-cli/codex-cli-automation-driver.md"]},
     ("active-host-doc-guidance",),
     ()),
    ("archived README", {"changed_files": ["docs/archive/README.md"]},
     ("product-entry-workflows",),
     ("state-and-boundary",)),
    ("changelog README", {"changed_files": ["docs/changelog/README.md"]},
     ("product-entry-workflows",),
     ()),
    ("nested archived Dev Book", {"changed_files": ["docs/archive/docs/book/en/index.md"]},
     (),
     ("state-and-boundary",)),
    ("nested changelog guard name", {"changed_files": ["docs/changelog/docs-active-host-guidance-smoke.py.md"]},
     (),
     ()),
    ("issue-fix outcome", {"changed_files": ["loopx/capabilities/issue_fix/repository_memory_provider.py",
                                             "examples/issue-fix-validated-memory-writeback-smoke.py"],
                           "surfaces": ["issue-fix outcome validated memory writeback"]},
     ("issue-fix-outcome-visibility", "product-entry-workflows", "repo-architecture-budget"),
     ("planning-governance",)),
    ("cross runtime", {"changed_files": ["loopx/control_plane/handoff/cross_runtime_impl_review.py",
                                         "loopx/cli_commands/starter.py",
                                         "docs/product/use-cases/cross-runtime/cross-runtime-impl-review-demo.md"],
                       "surfaces": ["loopx demo impl-review claude implements codex reviews "
                                    "cross_runtime_impl_review_demo_packet_v0"]},
     ("agent-facing-cli-output-budget", "cli-command-contract", "control-plane-refactor", "cross-runtime-impl-review-demo", "product-entry-workflows", "repo-architecture-budget"),
     ("work-routing",)),
    ("host command", {"changed_files": ["loopx/cli_commands/slash_commands.py",
                                        "docs/reference/protocols/global-manager-command-v0.md"],
                      "surfaces": ["slash-commands /loopx-global-summary host command registry"]},
     ("agent-facing-cli-output-budget", "cli-command-contract", "host-command-entry", "repo-architecture-budget"),
     ()),
    ("first connect", {"changed_files": ["loopx/bootstrap.py", "loopx/bootstrap_command_pack.py", "loopx/contract.py"],
                       "surfaces": ["new user onboarding first connect contract state projection gap start-goal"]},
     ("first-connect-contract", "repo-architecture-budget"),
     ("evidence-lifecycle", "human-decision", "state-and-boundary", "work-routing")),
    ("runtime connector", {"changed_files": ["docs/integrations/runtime-connector-catalog.md"],
                           "surfaces": ["runtime connector catalog codex app heartbeat codex cli tui claude code "
                                        "loop worker bridge scheduler_hint scoped identity"]},
     ("control-plane-state-machine", "monitor-scheduler", "runtime-connector-catalog", "scheduler-cadence"),
     ("state-and-boundary", "work-routing")),
    ("auto research", {"changed_files": ["demo/auto_research/core.py"],
                       "surfaces": ["auto-research demo frontier visible launcher"]},
     ("auto-research-demo",),
     ()),
    ("explore", {"changed_files": ["loopx/capabilities/explore/harness_runtime.py", "loopx/configure_goal.py"],
                 "surfaces": ["explore harness resume configure-goal"]},
     ("explore-harness", "peer-agent-runtime", "repo-architecture-budget"),
     ("human-decision",)),
    ("configure sync", {"changed_files": ["loopx/control_plane/goals/configure_goal_service.py"],
                        "surfaces": ["configure-goal authoritative shared runtime sync readback"]},
     ("peer-agent-runtime", "repo-architecture-budget"),
     ("state-and-boundary",)),
    ("benchmark toolkit", {"changed_files": ["loopx/capabilities/benchmark_toolkit/integrity.py"],
                           "surfaces": ["benchmark toolkit integrity no-submit boundary"]},
     ("benchmark-toolkit-boundary", "repo-architecture-budget"),
     ("evidence-lifecycle", "state-and-boundary")),
    ("frozen benchmark snapshot", {"changed_files": ["benchmark/deepswe-gptxhigh-v1/loopx_native_codex.py"]},
     (),
     ()),
    ("catalog canary", {"changed_files": ["loopx/canary/planner.py", "loopx/canary/runner.py"],
                        "surfaces": ["catalog canary runner"]},
     ("catalog-canary-contract", "repo-architecture-budget"),
     ()),
)


# Scripts each profile must keep scheduling, by tier, relative to examples/.
# Retiring one of these is a deliberate edit here, not a silent catalog drift.
REQUIRED_PROFILE_SCRIPTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "active-host-doc-guidance": (("docs-active-host-guidance-smoke.py",), ()),
    "auto-research-demo": (("auto-research-minimal-kernel-smoke.py", "decentralized-auto-research-frontier-smoke.py"), ()),
    "benchmark-toolkit-boundary": (("benchmark-run-permission-policy-smoke.py",), ()),
    "catalog-canary-contract": (("canary/catalog-planner-smoke.py", "canary/catalog-run-e2e-smoke.py", "canary/smoke-suite-runner-smoke.py"), ("canary/pytest-smoke-suite-facade-smoke.py",)),
    "cli-command-contract": (("cli-version-command-modularization-smoke.py",), ()),
    "control-plane-refactor": (("control_plane/bounded-context-namespace-smoke.py", "control_plane/quota-cleared-blocker-successor-gate-smoke.py", "control_plane/quota-resume-gated-open-todo-smoke.py"), ()),
    "control-plane-state-machine": (("control_plane/heartbeat-quota-flow-smoke.py", "control_plane/interaction-contract-state-machine-smoke.py", "control_plane/peer-agent-continuation-state-machine-smoke.py"), ("control_plane/control-plane-integrated-canary-smoke.py",)),
    "cross-runtime-impl-review-demo": (("cross-runtime-impl-review-demo-smoke.py", "public_entry/readme-demo-surface-smoke.py"), ()),
    "event-sourced-read-path": (("control_plane/event-sourced-downstream-read-path-smoke.py", "control_plane/event-sourced-state-api-smoke.py", "control_plane/event-sourced-status-read-path-smoke.py"), ()),
    "explore-harness": (("explore-configure-goal-smoke.py", "explore-harness-runtime-resume-smoke.py", "explore-worker-plan-gate-smoke.py"), ()),
    "goal-frontier-replan-rules": (("control_plane/goal-frontier-replan-rules-smoke.py",), ()),
    "host-command-entry": (("slash-command-catalog-smoke.py",), ()),
    "install-update": (("install-local-smoke.py", "loopx-update-smoke.py"), ("release/local-install-promotion-boundary-smoke.py",)),
    "issue-fix-outcome-visibility": (("issue-fix-outcome-projection-smoke.py", "issue-fix-validated-memory-writeback-smoke.py"), ()),
    "peer-agent-runtime": (("project/configure-goal-global-sync-smoke.py",), ()),
    "product-entry-workflows": (("content-ops-issue-fix-intake-smoke.py", "issue-fix-feasibility-smoke.py", "issue-fix-pr-lifecycle-smoke.py", "issue-fix-repository-context-smoke.py", "issue-fix-workflow-contract-smoke.py", "public_entry/readme-demo-surface-smoke.py", "update-notes-archive-smoke.py"), ()),
    "release-promotion": (("canary/canary-promotion-readiness-boundary-smoke.py", "control_plane/promotion-readiness-readmodel-smoke.py"), ("canary/canary-promotion-readiness-smoke.py",)),
    "repo-architecture-budget": (("control_plane/control-plane-maintainability-ratchet-smoke.py",), ()),
    "review-packet-read-path": (("control_plane/review-packet-cli-smoke.py",), ()),
    "runtime-connector-catalog": (("claude-goalmode-lifecycle-smoke.py", "codex-cli-tui-bootstrap-smoke-bundle-smoke.py", "control_plane/heartbeat-prompt-smoke.py"), ()),
    "scheduler-cadence": (("control_plane/monitor-scheduler-contract-smoke.py",), ()),
    "state-write-correctness": (("control_plane/task-lease-runtime-smoke.py", "control_plane/todo-write-correctness-smoke.py"), ()),
    "status-projection-cache": (("control_plane/status-projection-cache-smoke.py",), ()),
    "status-read-path": (("control_plane/goal-channel-readmodel-smoke.py", "control_plane/runtime-handoff-status-read-path-smoke.py", "control_plane/status-goal-filter-smoke.py", "control_plane/status-quota-review-packet-parity-smoke.py"), ()),
    "todo-lifecycle": (("control_plane/todo-lifecycle-cli-smoke.py",), ()),
}


def _domain_profiles(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    return {profile["id"]: profile for profile in payload["domain_profiles"]}  # type: ignore[index]


def _all_domain_profile_ids() -> list[str]:
    return [profile["id"] for profile in build_catalog_canary_profiles()["domain_profiles"]]


def assert_profiles_come_from_catalog_matrix() -> None:
    payload = build_catalog_canary_profiles()
    assert payload["ok"] is True, payload
    assert payload["dry_run"] is True, payload
    assert payload["executes_checks"] is False, payload
    families = {profile["family"] for profile in payload["profiles"]}
    assert {
        "Work Routing",
        "Human Decision",
        "State And Boundary",
        "Evidence Lifecycle",
        "Planning Governance",
    } <= families, payload
    work_routing = next(profile for profile in payload["profiles"] if profile["family"] == "Work Routing")
    assert "IP-001" in work_routing["pattern_ids"], work_routing
    assert work_routing["candidate_checks"], work_routing
    assert all("command" in check and "reason" in check for check in work_routing["candidate_checks"])
    for profile in payload["domain_profiles"]:
        assert profile["checks"], profile
        assert all(check["reason"] for check in profile["checks"]), profile
    selected_ids = set().union(*(expected for _, _, expected, _ in SELECTION_CASES))
    assert selected_ids <= set(_all_domain_profile_ids()), selected_ids - set(_all_domain_profile_ids())


def assert_every_planned_command_names_an_existing_script() -> None:
    """A retired or moved smoke must leave the catalog, not a dangling command."""

    profile_ids = _all_domain_profile_ids()
    domain = build_catalog_canary_plan(
        profiles=profile_ids, include_deep_checks=True, max_checks_per_profile=1000
    )
    assert domain["domain_profile_count"] == len(profile_ids), domain
    commands = [check["command"] for profile in domain["domain_profiles"] for check in profile["checks"]]
    family = build_catalog_canary_profiles()
    commands += [check["command"] for profile in family["profiles"] for check in profile["candidate_checks"]]
    missing = sorted(
        {
            token
            for command in commands
            for token in command.split()
            if SCRIPT_TOKEN.match(token) and "/" in token and not (REPO_ROOT / token).is_file()
        }
    )
    assert not missing, f"planned commands name missing scripts: {missing}"


def assert_plan_selects_minimal_profiles_from_changed_surfaces() -> None:
    payload = build_catalog_canary_plan(
        changed_files=["loopx/quota.py", "loopx/status.py"],
        surfaces=["scheduler hint", "quota should-run"],
        max_checks_per_family=2,
    )
    families = [profile["family"] for profile in payload["profiles"]]
    assert "Work Routing" in families, payload
    assert "State And Boundary" in families, payload
    assert "Evidence Lifecycle" not in families, payload
    for profile in payload["profiles"]:
        assert len(profile["candidate_checks"]) <= 2, profile
        assert profile["selection_reasons"], profile
    domain_profiles = _domain_profiles(payload)
    assert "control-plane-refactor" in domain_profiles, payload
    assert "monitor-scheduler" in domain_profiles, payload
    for profile in domain_profiles.values():
        assert all(check["tier"] == "default" for check in profile["checks"]), profile
        if profile["id"] == "repo-architecture-budget":
            assert profile["deep_checks_available"] is False, profile
        else:
            assert profile["deep_checks_available"] is True, profile
        assert profile["deep_checks_included"] is False, profile
    assert payload["suggested_check_count"] == len(payload["suggested_checks"]), payload
    assert payload["commands"] == [
        check["command"] for check in payload["suggested_checks"]
    ], payload
    assert payload["suggested_checks"][0]["source"] == "domain_profile", payload
    assert payload["executes_checks"] is False, payload


def assert_changed_surfaces_select_expected_profiles() -> None:
    for label, inputs, expected, expected_families in SELECTION_CASES:
        payload = build_catalog_canary_plan(**inputs)  # type: ignore[arg-type]
        selected = sorted(_domain_profiles(payload))
        assert selected == sorted(expected), (label, selected)
        families = sorted(profile["id"] for profile in payload["profiles"])
        assert families == sorted(expected_families), (label, families)
        for profile in payload["domain_profiles"]:
            assert profile["checks"], (label, profile)
            assert all(check["tier"] == "default" for check in profile["checks"]), (label, profile)
            assert profile["deep_checks_included"] is False, (label, profile)
        assert payload["commands"] == [check["command"] for check in payload["suggested_checks"]], label
        assert payload["executes_checks"] is False, label


def assert_profiles_keep_their_required_scripts() -> None:
    profile_ids = _all_domain_profile_ids()
    assert set(REQUIRED_PROFILE_SCRIPTS) <= set(profile_ids), set(REQUIRED_PROFILE_SCRIPTS) - set(profile_ids)
    plan = build_catalog_canary_plan(
        profiles=sorted(REQUIRED_PROFILE_SCRIPTS), include_deep_checks=True, max_checks_per_profile=1000
    )
    for profile_id, profile in _domain_profiles(plan).items():
        tiers = {check["command"]: check["tier"] for check in profile["checks"]}
        default, deep = REQUIRED_PROFILE_SCRIPTS[profile_id]
        for tier, scripts in (("default", default), ("deep", deep)):
            for script in scripts:
                command = f"python3 examples/{script}"
                assert tiers.get(command) == tier, (profile_id, command, tiers.get(command), tier)


def assert_deep_checks_are_opt_in_for_every_profile() -> None:
    for profile_id in _all_domain_profile_ids():
        default = build_catalog_canary_plan(profiles=[profile_id], max_checks_per_profile=1000)
        assert default["profile_count"] == 0, (profile_id, default)
        (profile,) = default["domain_profiles"]
        assert profile["id"] == profile_id, profile
        assert profile["deep_checks_included"] is False, profile
        assert all(check["tier"] == "default" for check in profile["checks"]), profile
        deep = build_catalog_canary_plan(
            profiles=[profile_id], include_deep_checks=True, max_checks_per_profile=1000
        )
        (deep_profile,) = deep["domain_profiles"]
        assert deep_profile["deep_checks_included"] is True, deep_profile
        deep_tiers = [check["tier"] for check in deep_profile["checks"] if check["tier"] != "default"]
        assert set(deep_tiers) <= {"deep"}, deep_profile
        assert bool(deep_tiers) is profile["deep_checks_available"], deep_profile
        default_commands = [check["command"] for check in profile["checks"]]
        deep_default_commands = [
            check["command"] for check in deep_profile["checks"] if check["tier"] == "default"
        ]
        assert deep_default_commands == default_commands, deep_profile
    note = build_catalog_canary_plan(profiles=[_all_domain_profile_ids()[0]], include_deep_checks=True)["note"]
    assert "existing public runtime/status contracts first" in note, note
    assert "owner-review necessity/risk packet" in note, note


def assert_explicit_catalog_profile_id_selects_family_profile() -> None:
    payload = build_catalog_canary_plan(
        profiles=["state-and-boundary"],
        max_checks_per_family=4,
    )
    assert payload["profile_count"] == 1, payload
    assert payload["domain_profile_count"] == 0, payload
    profile = payload["profiles"][0]
    assert profile["id"] == "state-and-boundary", profile
    assert profile["family"] == "State And Boundary", profile
    assert profile["selection_reasons"] == [
        "selected because this catalog profile was explicitly requested",
    ], profile
    assert payload["commands"], payload
    assert all(
        check["source"] == "catalog_family"
        for check in payload["suggested_checks"]
    ), payload


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _make_git_diff_selector_repo(tmp_dir: Path) -> tuple[Path, str]:
    repo = tmp_dir / "selector-repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "loopx-smoke@example.invalid")
    _run_git(repo, "config", "user.name", "LoopX Smoke")

    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "base")
    base_ref = _run_git(repo, "rev-parse", "HEAD").stdout.strip()

    committed_path = repo / "loopx" / "canary" / "planner.py"
    committed_path.parent.mkdir(parents=True)
    committed_path.write_text("# committed canary planner change\n", encoding="utf-8")
    _run_git(repo, "add", "loopx/canary/planner.py")
    _run_git(repo, "commit", "-m", "committed canary planner")

    unstaged_path = repo / "examples" / "canary" / "catalog-planner-smoke.py"
    unstaged_path.parent.mkdir(parents=True)
    unstaged_path.write_text("# tracked catalog canary smoke change\n", encoding="utf-8")
    _run_git(repo, "add", "examples/canary/catalog-planner-smoke.py")
    _run_git(repo, "commit", "-m", "tracked catalog canary smoke")

    staged_path = repo / "loopx" / "cli_commands" / "canary.py"
    staged_path.parent.mkdir(parents=True)
    staged_path.write_text("# staged cli canary change\n", encoding="utf-8")
    _run_git(repo, "add", "loopx/cli_commands/canary.py")

    unstaged_path.write_text("# unstaged catalog canary smoke change\n", encoding="utf-8")

    untracked_path = repo / "docs" / "new-catalog-canary-note.md"
    untracked_path.parent.mkdir(parents=True)
    untracked_path.write_text("# untracked catalog canary note\n", encoding="utf-8")
    return repo, base_ref


def assert_git_diff_selector_covers_pr_and_worktree_changes(tmp_dir: Path) -> None:
    repo, base_ref = _make_git_diff_selector_repo(tmp_dir)
    assert _run_git(repo, "diff", "--name-only", "--cached").stdout.splitlines() == [
        "loopx/cli_commands/canary.py",
    ]
    assert _run_git(repo, "diff", "--name-only").stdout.splitlines() == [
        "examples/canary/catalog-planner-smoke.py",
    ]
    assert _run_git(repo, "ls-files", "--others", "--exclude-standard").stdout.splitlines() == [
        "docs/new-catalog-canary-note.md",
    ]
    selector = collect_git_diff_changed_files(repo_root=repo, base_ref=base_ref)
    assert selector["ok"] is True, selector
    assert selector["successful_sources"] == ["base", "staged", "unstaged", "untracked"], selector
    assert selector["changed_files"] == [
        "examples/canary/catalog-planner-smoke.py",
        "loopx/canary/planner.py",
        "loopx/cli_commands/canary.py",
        "docs/new-catalog-canary-note.md",
    ], selector

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "--format",
            "json",
            "canary",
            "plan",
            "--from-git-diff",
            "--git-diff-base",
            base_ref,
        ],
        cwd=repo,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    payload = json.loads(completed.stdout)
    assert payload["selector_sources"]["git_diff"]["changed_file_count"] == 4, payload
    assert payload["selection_inputs"]["changed_files"] == selector["changed_files"], payload
    domain_profile_ids = {profile["id"] for profile in payload["domain_profiles"]}
    assert "catalog-canary-contract" in domain_profile_ids, payload
    assert "benchmark-toolkit-boundary" not in domain_profile_ids, payload


def assert_coverage_audit_tracks_p0_p1_patterns() -> None:
    payload = build_catalog_canary_coverage_audit()
    assert payload["ok"] is True, payload
    assert payload["dry_run"] is True, payload
    assert payload["executes_checks"] is False, payload
    assert payload["priorities"] == ["P0", "P1"], payload
    assert payload["required_pattern_count"] >= 20, payload
    assert payload["missing_count"] == 0, payload
    assert payload["invalid_exception_count"] == 0, payload
    covered_ids = {row["pattern_id"] for row in payload["covered_patterns"]}
    assert {"IP-001", "IP-004", "IP-024", "IP-029"} <= covered_ids, payload


def assert_coverage_audit_reports_matrix_drift(tmp_dir: Path) -> None:
    catalog_text = CATALOG.read_text(encoding="utf-8")
    # Mutate one coverage token, independently of unrelated catalog additions.
    rows = catalog_text.splitlines(keepends=True)
    matching = [
        index for index, row in enumerate(rows)
        if row.startswith("| Planning Governance |")
        and "IP-024" in [pattern.strip() for pattern in row.split("|")[2].split(",")]
    ]
    assert len(matching) == 1, "expected one Planning Governance coverage row"
    index = matching[0]
    cells = rows[index].split("|")
    patterns = [pattern.strip() for pattern in cells[2].split(",")]
    assert patterns.count("IP-024") == 1, "drift probe requires exactly one IP-024 token"
    cells[2] = " " + ", ".join(pattern for pattern in patterns if pattern != "IP-024") + " "
    rows[index] = "|".join(cells)
    drift_text = "".join(rows)
    assert drift_text != catalog_text, "drift probe must change its input"
    drift_catalog = tmp_dir / "catalog-drift.md"
    drift_catalog.write_text(drift_text, encoding="utf-8")
    payload = build_catalog_canary_coverage_audit(catalog_path=drift_catalog)
    assert payload["ok"] is False, payload
    assert payload["missing_count"] == 1, payload
    assert payload["missing_patterns"][0]["pattern_id"] == "IP-024", payload

    excepted_catalog = tmp_dir / "catalog-deferred.md"
    excepted_catalog.write_text(
        drift_text
        + "\n\n"
        "## Canary Coverage Exceptions\n\n"
        "| Pattern ID | Canary Coverage Status | Rationale | Owner |\n"
        "| --- | --- | --- | --- |\n"
        "| IP-024 | deferred | waits for a repair-delta profile owner before default canary coverage | codex-main-control |\n",
        encoding="utf-8",
    )
    excepted_payload = build_catalog_canary_coverage_audit(catalog_path=excepted_catalog)
    assert excepted_payload["ok"] is True, excepted_payload
    assert excepted_payload["missing_count"] == 0, excepted_payload
    assert excepted_payload["excepted_count"] == 1, excepted_payload
    assert excepted_payload["excepted_patterns"][0]["pattern_id"] == "IP-024", excepted_payload


def assert_cli_json_plan_is_dry_run() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "--format",
            "json",
            "canary",
            "plan",
            "--changed-file",
            "loopx/quota.py",
            "--surface",
            "scheduler hint",
            "--max-checks-per-family",
            "1",
        ],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    payload = json.loads(completed.stdout)
    assert payload["dry_run"] is True, payload
    assert payload["executes_checks"] is False, payload
    assert payload["profile_count"] >= 1, payload
    assert payload["suggested_check_count"] == len(payload["commands"]), payload
    assert payload["commands"], payload
    assert all(check["command"] in payload["commands"] for check in payload["suggested_checks"]), payload
    work_routing = next(profile for profile in payload["profiles"] if profile["family"] == "Work Routing")
    assert len(work_routing["candidate_checks"]) == 1, work_routing
    assert any(profile["id"] == "monitor-scheduler" for profile in payload["domain_profiles"]), payload


def assert_cli_profile_accepts_catalog_profile_id() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "--format",
            "json",
            "canary",
            "plan",
            "--profile",
            "state-and-boundary",
            "--max-checks-per-family",
            "1",
        ],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    payload = json.loads(completed.stdout)
    assert payload["profile_count"] == 1, payload
    assert payload["domain_profile_count"] == 0, payload
    assert payload["profiles"][0]["id"] == "state-and-boundary", payload
    assert len(payload["commands"]) == 1, payload


def assert_cli_json_coverage_audit_is_dry_run() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "--format",
            "json",
            "canary",
            "coverage-audit",
        ],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    payload = json.loads(completed.stdout)
    assert payload["dry_run"] is True, payload
    assert payload["executes_checks"] is False, payload
    assert payload["drift_count"] == 0, payload


def main() -> int:
    assert_profiles_come_from_catalog_matrix()
    assert_every_planned_command_names_an_existing_script()
    assert_plan_selects_minimal_profiles_from_changed_surfaces()
    assert_changed_surfaces_select_expected_profiles()
    assert_profiles_keep_their_required_scripts()
    assert_deep_checks_are_opt_in_for_every_profile()
    assert_explicit_catalog_profile_id_selects_family_profile()
    assert_coverage_audit_tracks_p0_p1_patterns()
    tmp = tempfile.mkdtemp(prefix="loopx-catalog-canary-smoke-")
    try:
        tmp_dir = Path(tmp)
        assert_coverage_audit_reports_matrix_drift(tmp_dir)
        assert_git_diff_selector_covers_pr_and_worktree_changes(tmp_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert_cli_json_plan_is_dry_run()
    assert_cli_profile_accepts_catalog_profile_id()
    assert_cli_json_coverage_audit_is_dry_run()
    print("catalog-canary-planner-smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
