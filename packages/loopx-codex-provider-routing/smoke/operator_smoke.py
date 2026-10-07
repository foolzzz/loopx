"""Offline regression tests for the opt-in local operator (no real credentials)."""

from __future__ import annotations

# ruff: noqa: E402 -- resolve this checkout before importing the package

import contextlib
import io
import json
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from loopx_codex_provider_routing.operator import rollback, run, snapshot
from loopx_codex_provider_routing.operator_catalog import CLIModelCatalog
from loopx_codex_provider_routing.operator_runtime import (
    CPAOperator,
    sha256,
    write_private,
)
from loopx_codex_provider_routing.operator_settings import OperatorSettings
from loopx_codex_provider_routing.selectors import (
    ROUTES,
    aliases_for_slot,
    compiled_routes,
    routing_source,
)


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cpa-operator-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        runtime = self.root / "runtime"
        paths = {key: str(self.root / key) for key in OperatorSettings.PATH_KEYS}
        paths.update(
            runtime_root=str(runtime), temporary_root=str(self.root / "temporary")
        )
        self.data = {
            "schema_version": "loopx_cpa_local_operator_v2",
            "paths": paths,
            "binary_sha256": "0" * 64,
            "source_commit": "0" * 40,
            "port": 19876,
            "launchd_label": "org.example.cpa-test",
            "profile_name": "cpa-native",
            "cpa_client_env_key": "CPA_CLIENT_KEY",
            "fallback_routes": [],
        }
        self.data["paths"]["codex_home"] = str(self.root / "codex-home")
        self.settings = OperatorSettings(self.data)
        self.runtime = CPAOperator(self.settings)
        self.config = self.root / "operator.json"
        write_private(self.config, json.dumps(self.data))

    def seed(self):
        r = self.runtime
        r.AUTH_DIR.mkdir(parents=True)
        slots = {}
        for slot in "abc":
            name = f"{slot}.json"
            write_private(
                r.AUTH_DIR / name,
                json.dumps(
                    {
                        "type": "codex",
                        "account_id": f"fixture-{slot}",
                        "access_token": "fixture-access",
                        "refresh_token": "fixture-refresh",
                    }
                ),
            )
            r.patch_slot_auth(r.AUTH_DIR / name, slot)
            slots[slot] = name
        write_private(r.SLOTS_FILE, json.dumps(slots))
        write_private(r.MODEL_CATALOG, json.dumps({"models": []}))

    def test_default_is_plan_without_files_or_processes(self):
        for command in ("serve", "write-profile", "install-profile", "probe"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(run(["--config", str(self.config), command]), 0)
            self.assertFalse(self.runtime.RUNTIME_ROOT.exists())
            receipt = json.loads(output.getvalue())
            self.assertFalse(receipt["executed"])
            self.assertNotIn(str(self.root), output.getvalue())

    def test_private_configuration_and_exact_targets(self):
        self.config.chmod(0o644)
        with self.assertRaises(ValueError):
            OperatorSettings.read(self.config)
        self.data["paths"]["runtime_root"] = "/"
        with self.assertRaises(ValueError):
            OperatorSettings(self.data)

    def test_reject_state_inside_git_worktree(self):
        (self.root / ".git").mkdir()
        with self.assertRaises(ValueError):
            OperatorSettings(self.data)

    def test_reject_symlink_state(self):
        self.runtime.RUNTIME_ROOT.mkdir()
        other = self.root / "other"
        other.mkdir()
        self.runtime.AUTH_DIR.symlink_to(other)
        with self.assertRaises(ValueError):
            self.runtime.check_target_boundaries()

    def test_reject_slot_escape_and_duplicate_credentials(self):
        for slots in (
            {"a": "../outside.json"},
            {"a": "a.json", "b": "a.json"},
            {"d": "d.json"},
        ):
            write_private(self.runtime.SLOTS_FILE, json.dumps(slots))
            with self.assertRaises(ValueError):
                self.runtime.load_slots()

    def test_three_account_ring_and_model_parity(self):
        expected = {
            "auto": list("abc"),
            "codex-a": list("abc"),
            "codex-b": list("bca"),
            "codex-c": list("cab"),
        }
        for model in ("gpt-5.6-sol", "gpt-6-astra"):
            for prefix, order in expected.items():
                for fast in (False, True):
                    slug = ("fast/" if fast else "") + prefix + "/" + model
                    entries = {
                        slot: next(
                            e for e in aliases_for_slot(slot) if e["alias"] == slug
                        )
                        for slot in "abc"
                    }
                    actual = sorted(
                        entries, key=lambda slot: -entries[slot]["routing-priority"]
                    )
                    self.assertEqual(actual, order)
                    self.assertTrue(all(e["name"] == model for e in entries.values()))
                    self.assertEqual(ROUTES[slug]["tail"], [])
                    if prefix == "auto" and not fast:
                        self.assertTrue(all(e["fork"] for e in entries.values()))
        self.assertEqual(ROUTES["gpt-5.6-luna"]["tail"], [])
        compiled = compiled_routes()
        for row in compiled["selector_rows"]:
            expected_tail = (
                ["ark-text"] if row["slug"].startswith("auto-with-ds/") else []
            )
            self.assertEqual(
                [p for p in row["candidates"] if p == "ark-text"], expected_tail
            )

    def test_native_only_requires_no_ark_key_or_plugin(self):
        with patch.dict("os.environ", {"ARK_API_KEY": "must-not-be-read"}):
            self.assertEqual(self.runtime.load_ark_key(None), "")
        config = self.runtime.runtime_config("", management_secret="fixture")
        self.assertNotIn("openai-compatibility", config)
        self.assertNotIn("plugins:", config)
        self.assertFalse(
            any(
                entry["alias"].startswith("auto-with-ds/")
                for entry in self.runtime.aliases_for_slot("a")
            )
        )

    def test_cooldown_reset_is_scoped_and_not_a_health_claim(self):
        self.seed()
        entry = {"name": "b.json", "auth_index": "fixture-index", "disabled": False}
        with (
            patch.object(
                self.runtime, "fetch_management_auth_files", return_value=[entry]
            ),
            patch("urllib.request.urlopen") as urlopen,
        ):
            urlopen.return_value.__enter__.return_value = io.StringIO('{"status":"ok"}')
            result = self.runtime.reset_cooldown("b")
            request = urlopen.call_args.args[0]
            self.assertTrue(request.full_url.endswith("/v0/management/reset-quota"))
            self.assertEqual(json.loads(request.data), {"auth_index": "fixture-index"})
            self.assertEqual(result["profile_id"], "codex-b")
            self.assertTrue(result["live_verification_required"])
            self.assertNotIn("fixture-index", json.dumps(result))

    def test_cooldown_reset_rejects_disabled_missing_and_ambiguous_slots(self):
        self.seed()
        for entries in (
            [],
            [{"name": "b.json", "disabled": True, "auth_index": "fixture"}],
            [{"name": "b.json"}],
            [{"name": "b.json"}, {"name": "b.json"}],
        ):
            with (
                patch.object(
                    self.runtime, "fetch_management_auth_files", return_value=entries
                ),
                patch("urllib.request.urlopen") as urlopen,
            ):
                with self.assertRaises(RuntimeError):
                    self.runtime.reset_cooldown("b")
                urlopen.assert_not_called()

    def test_cooldown_reset_requires_slot_and_explicit_execution(self):
        with self.assertRaises(ValueError):
            run(["--config", str(self.config), "reset-cooldown"])
        with (
            patch.object(CPAOperator, "reset_cooldown") as reset,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                run(["--config", str(self.config), "reset-cooldown", "--slot", "b"]), 0
            )
            reset.assert_not_called()

    def test_reconcile_preserves_tokens_and_is_idempotent(self):
        self.seed()
        path = self.runtime.AUTH_DIR / "c.json"
        before = path.read_bytes()
        self.runtime.patch_slot_auth(path, "c")
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(json.loads(before)["refresh_token"], "fixture-refresh")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rollback_preserves_rotated_credentials(self):
        self.seed()
        backup = snapshot(self.runtime)
        path = self.runtime.AUTH_DIR / "a.json"
        data = json.loads(path.read_text())
        data.update(
            refresh_token="fixture-refreshed",
            access_token="fixture-new-access",
            priority=1,
        )
        write_private(path, json.dumps(data))
        rollback(self.runtime, backup)
        restored = json.loads(path.read_text())
        self.assertEqual(restored["priority"], 400)
        self.assertEqual(restored["refresh_token"], "fixture-refreshed")
        self.assertEqual(restored["access_token"], "fixture-new-access")

    def test_rollback_rejects_tampering_before_any_write(self):
        self.seed()
        backup = snapshot(self.runtime)
        before = self.runtime.SLOTS_FILE.read_bytes()
        directory = self.runtime.STATE_DIR / "operator-backups" / backup
        (directory / "0.json").write_text("{}")
        with self.assertRaises(ValueError):
            rollback(self.runtime, backup)
        self.assertEqual(before, self.runtime.SLOTS_FILE.read_bytes())
        with self.assertRaises(ValueError):
            rollback(self.runtime, "../outside")

    def metadata(self):
        return {
            "schema_version": "codex_cli_model_metadata_v1",
            "codex_cli_version": "0.160.0",
            "source_kind": "synthetic",
            "models": [
                {
                    "slug": model,
                    "display_name": model,
                    "description": "Synthetic model metadata",
                    "input_modalities": ["text", "image"],
                    "default_reasoning_level": "high",
                    "supported_reasoning_levels": [
                        {"effort": effort, "description": effort}
                        for effort in ("low", "medium", "high", "xhigh")
                    ],
                    "additional_speed_tiers": ["fast"],
                    "service_tiers": [
                        {
                            "id": "priority",
                            "name": "Fast",
                            "description": "Synthetic tier",
                        }
                    ],
                    "context_window": 100000,
                }
                for model in ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-6-astra")
            ],
        }

    def seed_plan(self):
        source = routing_source()
        source["profiles"] = [
            member for member in source["profiles"] if member["id"].startswith("codex-")
        ]
        source["routes"] = [
            route for route in source["routes"] if not route.get("fallback_tail")
        ]
        plan = {
            "catalog_source": source,
            "route_id": "native-route",
            "routing_revision": "v1",
            "deployment_ref": "cpa-native-v1",
            "provider_id": "cpa",
            "model_selector": "auto/gpt-5.6-sol",
            "reasoning_effort": "high",
            "service_tier": "default",
            "required_capabilities": {
                "modalities": ["text"],
                "tool_transport": "custom_tool_call",
            },
            "codex_version_requirement": "0.160.0",
        }
        write_private(
            self.settings.paths["route_plan"],
            json.dumps(
                {
                    "schema_version": "loopx_codex_provider_routing_request_v1",
                    "operation": "compile_cli_plan",
                    "cli_plan": plan,
                }
            ),
        )
        write_private(
            self.settings.paths["model_metadata"], json.dumps(self.metadata())
        )

    def test_auth_rollback_holds_running_or_unknown_writer_before_any_write(self):
        self.seed()
        backup = snapshot(self.runtime)
        write_private(self.runtime.MODEL_CATALOG, '{"new": true}')
        original = {
            path: path.read_bytes()
            for path in (
                self.runtime.MODEL_CATALOG,
                self.runtime.SLOTS_FILE,
                self.runtime.AUTH_DIR / "a.json",
            )
        }
        for pid_content, alive in (("4242", True), ("unknown", False), ("0", False)):
            write_private(self.runtime.PID_FILE, pid_content)
            with (
                patch.object(self.runtime, "pid_alive", return_value=alive),
                self.assertRaisesRegex(RuntimeError, "credential writer"),
            ):
                rollback(self.runtime, backup)
            self.assertTrue(
                all(path.read_bytes() == content for path, content in original.items())
            )

    def test_profile_snapshot_rollback_does_not_touch_live_auth_membership(self):
        self.seed()
        self.seed_plan()
        original = self.runtime.SLOTS_FILE.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            run(["--config", str(self.config), "--execute", "write-profile"])
        installed = self.runtime.INSTALLED_PROFILE
        write_private(installed, CLIModelCatalog(self.runtime).profile_content())
        with patch.object(self.runtime, "pid_alive", return_value=True) as alive:
            rollback(self.runtime, json.loads(output.getvalue())["rollback_snapshot"])
            alive.assert_not_called()
        self.assertFalse(self.runtime.PROFILE_FILE.exists())
        self.assertTrue(installed.exists())
        self.assertEqual(self.runtime.SLOTS_FILE.read_bytes(), original)

    def test_catalog_uses_versioned_native_metadata(self):
        self.seed_plan()
        catalog = CLIModelCatalog(self.runtime)
        rows = {row["slug"]: row for row in catalog.generate_catalog()["models"]}
        self.assertEqual(len(rows), 19)
        self.assertFalse(any(slug.startswith("auto-with-ds/") for slug in rows))
        for slug, row in rows.items():
            self.assertEqual(row["input_modalities"], ["text", "image"])
            self.assertEqual(
                row["default_service_tier"],
                "fast" if slug.startswith("fast/") else None,
            )
        catalog.write_catalog()
        first = sha256(self.runtime.MODEL_CATALOG)
        catalog.write_catalog()
        self.assertEqual(first, sha256(self.runtime.MODEL_CATALOG))

    def test_plan_rejects_inconsistent_operator_candidates_before_writing(self):
        self.seed_plan()
        request = json.loads(self.settings.paths["route_plan"].read_text())
        source = request["cli_plan"]["catalog_source"]
        source["profiles"] = source["profiles"][:2]
        source["rings"][0]["members"] = ["codex-a", "codex-b"]
        source["routes"] = [
            route for route in source["routes"] if route["slug"] == "auto/gpt-5.6-sol"
        ]
        write_private(self.settings.paths["route_plan"], json.dumps(request))
        with self.assertRaisesRegex(ValueError, "operator routing preset"):
            CLIModelCatalog(self.runtime).write_profile()
        self.assertFalse(self.runtime.RUNTIME_ROOT.exists())

    def test_metadata_rejects_unknown_and_prompt_fields(self):
        from copy import deepcopy

        self.seed_plan()
        for key in ("prompt", "raw_body", "base_instructions", "unknown"):
            metadata = deepcopy(self.metadata())
            metadata["models"][0][key] = "synthetic"
            write_private(self.settings.paths["model_metadata"], json.dumps(metadata))
            with self.assertRaises(ValueError):
                CLIModelCatalog(self.runtime).generate_catalog()

        for field, value in (
            ("context_window", {"raw_body": "synthetic"}),
            (
                "service_tiers",
                [{"id": "priority", "description": {"prompt": "synthetic"}}],
            ),
            ("upgrade", {"prompt": "synthetic"}),
        ):
            metadata = deepcopy(self.metadata())
            metadata["models"][0][field] = value
            write_private(self.settings.paths["model_metadata"], json.dumps(metadata))
            with self.assertRaises(ValueError):
                CLIModelCatalog(self.runtime).generate_catalog()

    def test_independent_profile_apply_readback_and_rollback(self):
        import tomllib

        self.seed_plan()
        self.runtime.INSTALLED_PROFILE.parent.mkdir()
        untouched = {}
        for name in (
            "config.toml",
            "auth.json",
            "sessions/existing",
            "automations/existing",
        ):
            target = self.runtime.INSTALLED_PROFILE.parent / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("synthetic original")
            untouched[target] = target.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                run(["--config", str(self.config), "--execute", "install-profile"]), 0
            )
        receipt = json.loads(output.getvalue())
        profile = tomllib.loads(self.runtime.INSTALLED_PROFILE.read_text())
        self.assertNotIn("profiles", profile)
        self.assertEqual(profile["model"], "auto/gpt-5.6-sol")
        self.assertEqual(profile["model_providers"]["cpa"]["env_key"], "CPA_CLIENT_KEY")
        self.assertEqual(profile["model_providers"]["cpa"]["request_max_retries"], 0)
        rollback(self.runtime, receipt["rollback_snapshot"])
        self.assertFalse(self.runtime.INSTALLED_PROFILE.exists())
        for target, content in untouched.items():
            self.assertEqual(target.read_bytes(), content)
        self.assertNotIn(str(self.root), output.getvalue())

    def test_install_refuses_existing_unrelated_profile(self):
        self.seed_plan()
        write_private(self.runtime.INSTALLED_PROFILE, 'model = "owner-choice"')
        with self.assertRaises(ValueError):
            CLIModelCatalog(self.runtime).write_profile(install=True)
        self.assertEqual(
            self.runtime.INSTALLED_PROFILE.read_text(), 'model = "owner-choice"'
        )

    def test_rollback_retains_modified_installed_profile(self):
        self.seed_plan()
        catalog = CLIModelCatalog(self.runtime)
        backup = snapshot(self.runtime, profile_content=catalog.profile_content())
        catalog.write_profile(install=True)
        write_private(self.runtime.INSTALLED_PROFILE, 'model = "owner-change"')
        with self.assertRaises(ValueError):
            rollback(self.runtime, backup)
        self.assertTrue(self.runtime.INSTALLED_PROFILE.exists())

    def test_target_roots_reject_codex_stores_and_ancestor_symlinks(self):
        from copy import deepcopy

        for target in (
            self.root / ".codex",
            self.root / "Codex.app" / "data",
            self.root / "sessions",
            self.root / "automations",
        ):
            data = deepcopy(self.data)
            data["paths"]["runtime_root"] = str(target)
            with self.assertRaises(ValueError):
                OperatorSettings(data)
        linked = self.root / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        data = deepcopy(self.data)
        data["paths"]["runtime_root"] = str(linked / "runtime")
        with self.assertRaises(ValueError):
            OperatorSettings(data)

    def test_failed_reconcile_does_not_patch_earlier_slots(self):
        from unittest.mock import patch

        self.seed()
        path = self.runtime.AUTH_DIR / "a.json"
        original = path.read_bytes()
        (self.runtime.AUTH_DIR / "c.json").unlink()
        with patch.object(self.runtime, "prepare"), self.assertRaises(ValueError):
            self.runtime.reconcile()
        self.assertEqual(path.read_bytes(), original)

    def test_rollback_rejects_changed_identity_before_writes(self):
        self.seed()
        backup = snapshot(self.runtime)
        write_private(self.runtime.MODEL_CATALOG, '{"new": true}')
        path = self.runtime.AUTH_DIR / "c.json"
        data = json.loads(path.read_text())
        data["account_id"] = "fixture-replaced"
        write_private(path, json.dumps(data))
        with self.assertRaises(ValueError):
            rollback(self.runtime, backup)
        self.assertEqual(self.runtime.MODEL_CATALOG.read_text(), '{"new": true}')

        path.unlink()
        with self.assertRaises(ValueError):
            rollback(self.runtime, backup)
        self.assertFalse(path.exists())
        self.assertEqual(self.runtime.MODEL_CATALOG.read_text(), '{"new": true}')

    def test_rollback_deactivates_new_enrollment_without_deleting_tokens(self):
        self.seed()
        write_private(
            self.runtime.SLOTS_FILE, json.dumps({"a": "a.json", "b": "b.json"})
        )
        backup = snapshot(self.runtime)
        write_private(
            self.runtime.SLOTS_FILE,
            json.dumps({"a": "a.json", "b": "b.json", "c": "c.json"}),
        )
        rollback(self.runtime, backup)
        data = json.loads((self.runtime.AUTH_DIR / "c.json").read_text())
        self.assertTrue(data["disabled"])
        self.assertEqual(data["refresh_token"], "fixture-refresh")
        self.assertNotIn("c", self.runtime.load_slots())

    def test_config_fallback_requires_explicit_route_opt_in(self):
        from copy import deepcopy

        data = deepcopy(self.data)
        data.update(
            fallback_routes=["auto-with-ds/gpt-6-astra"],
            ark_base_url="https://api.example.invalid/v1",
            ark_model="deepseek-v4-flash-ga-260731",
            ark_pro_model="deepseek-v4-pro-ga-260813",
        )
        data["paths"]["ark_env_file"] = str(self.root / "ark-env")
        runtime = CPAOperator(OperatorSettings(data))
        config = runtime.runtime_config(
            "fixture-only", management_secret="fixture-management"
        )
        self.assertIn('alias: "auto-with-ds/gpt-6-astra"', config)
        self.assertNotIn('alias: "auto-with-ds/gpt-5.6-sol"', config)
        self.assertNotIn('alias: "codex-c/gpt-6-astra"', config)
        self.assertNotIn('alias: "fast/', config)
        self.assertIn('host: "127.0.0.1"', config)


if __name__ == "__main__":
    unittest.main()
