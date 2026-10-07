"""Compile explicit CLI model metadata and read back an isolated CLI catalog."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

from .model_metadata import CODEX_VERSION, model_source, read_metadata
from .operator_runtime import write_private
from .selectors import MODEL_FAMILIES, ROUTES, VISIBLE_SELECTORS, routing_source

AUTO_REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def read_messages(
    process: subprocess.Popen[str], wanted_id: int, timeout: float
) -> dict[str, Any]:
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("CLI app-server exited before catalog readback")
            if not selector.select(
                timeout=min(0.2, max(0, deadline - time.monotonic()))
            ):
                continue
            line = process.stdout.readline()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == wanted_id:
                return message
    raise TimeoutError("CLI app-server catalog readback timed out")


def render_cli_profile(
    plan: dict, catalog_path: Path, endpoint: str, env_key: str
) -> str:
    """Render only bounded route knobs; permission and tool settings stay host-owned."""
    provider = plan["provider_id"]
    lines = [
        f"model = {json.dumps(plan['model_selector'])}",
        f"model_provider = {json.dumps(provider)}",
        f"model_reasoning_effort = {json.dumps(plan['reasoning_effort'])}",
        f"service_tier = {json.dumps(plan['service_tier'])}",
        f"model_catalog_json = {json.dumps(str(catalog_path))}",
        "",
        f"[model_providers.{json.dumps(provider)}]",
        'name = "CPA"',
        f"base_url = {json.dumps(endpoint)}",
        f"env_key = {json.dumps(env_key)}",
        'wire_api = "responses"',
        "requires_openai_auth = false",
        "supports_websockets = false",
        "request_max_retries = 0",
        "stream_max_retries = 0",
        "",
    ]
    return "\n".join(lines)


class CLIModelCatalog:
    def __init__(self, runtime):
        self.runtime = runtime
        self.settings = runtime.settings
        self.CODEX_BINARY = self.settings.paths["codex_binary"]
        self.OUTPUT = runtime.MODEL_CATALOG

    def generate_catalog(self) -> dict[str, Any]:
        metadata = read_metadata(self.settings.paths["model_metadata"])
        active_fallbacks = set(self.settings.data["fallback_routes"])
        entries = []
        for slug, route in ROUTES.items():
            if route["tail"] and slug not in active_fallbacks:
                continue
            source = model_source(metadata, route["model"])
            if slug.removeprefix("fast/").startswith(("auto/", "auto-with-ds/")):
                source["default_reasoning_level"] = "high"
                source["supported_reasoning_levels"] = [
                    row
                    for row in source["supported_reasoning_levels"]
                    if row["effort"] in AUTO_REASONING_EFFORTS
                ]
            source.update(
                slug=slug,
                display_name=route["display_name"],
                description=route["display_name"],
                priority=len(entries) + 1,
                visibility="list" if slug in VISIBLE_SELECTORS else "hide",
                upgrade=None,
                default_service_tier="fast" if slug.startswith("fast/") else None,
            )
            if route["tail"]:
                source.update(additional_speed_tiers=[], service_tiers=[])
            entries.append(source)
        for model, label in MODEL_FAMILIES.items():
            source = model_source(metadata, model)
            source.update(
                slug=model,
                display_name=label,
                description=label,
                priority=len(entries) + 1,
                visibility="hide",
                upgrade=None,
                default_service_tier=None,
            )
            entries.append(source)
        if active_fallbacks:
            for slug in (self.runtime.ARK_MODEL, self.runtime.ARK_PRO_MODEL):
                source = model_source(metadata, slug)
                source.update(
                    priority=len(entries) + 1,
                    visibility="hide",
                    upgrade=None,
                    default_service_tier=None,
                )
                entries.append(source)
        return {"models": entries}

    def write_catalog(self) -> None:
        self.runtime.check_target_boundaries()
        write_private(
            self.OUTPUT,
            json.dumps(self.generate_catalog(), ensure_ascii=False, indent=2) + "\n",
        )

    def launch_plan(self) -> dict:
        from .contract import compile_cli_plan

        request = json.loads(
            self.settings.paths["route_plan"].read_text(encoding="utf-8")
        )
        from .schema_contract import validate_request

        validate_request(request)
        if request["operation"] != "compile_cli_plan":
            raise ValueError("route_plan requires a compile_cli_plan request")
        plan = compile_cli_plan(request["cli_plan"])
        canonical_input = dict(request["cli_plan"], catalog_source=routing_source())
        canonical = compile_cli_plan(canonical_input)
        if (
            plan["eligible_candidates"] != canonical["eligible_candidates"]
            or plan["service_tier"] != canonical["service_tier"]
        ):
            raise ValueError(
                "route plan differs from the configured operator routing preset"
            )
        if plan["model_selector"] not in {
            row["slug"] for row in self.generate_catalog()["models"]
        }:
            raise ValueError("route selector is absent from the operator catalog")
        selected = next(
            row
            for row in self.generate_catalog()["models"]
            if row["slug"] == plan["model_selector"]
        )
        if plan["reasoning_effort"] not in {
            row["effort"] for row in selected["supported_reasoning_levels"]
        } or not set(plan["required_capabilities"]["modalities"]) <= set(
            selected["input_modalities"]
        ):
            raise ValueError(
                "route plan capabilities differ from explicit CLI metadata"
            )
        if (
            plan["model_selector"].startswith("fast/")
            and self.runtime.FAST_SELECTOR_PLUGIN is None
        ):
            raise ValueError("active Fast selector requires a pinned selector plugin")
        return plan

    def profile_content(self, catalog_path: Path | None = None) -> str:
        return render_cli_profile(
            self.launch_plan(),
            catalog_path or self.OUTPUT,
            f"http://127.0.0.1:{self.runtime.PORT}/v1",
            self.settings.data["cpa_client_env_key"],
        )

    def write_profile(self, *, install: bool = False) -> None:
        self.runtime.check_target_boundaries()
        target = (
            self.runtime.INSTALLED_PROFILE if install else self.runtime.PROFILE_FILE
        )
        if target is None:
            raise ValueError("profile installation requires explicit codex_home")
        content = self.profile_content()
        # Generated output remains the source for installations, never an arbitrary
        # user profile carrying hooks, MCP or permission overrides.
        if (
            install
            and target.exists()
            and target.read_text(encoding="utf-8") != content
        ):
            raise ValueError(
                "existing profile differs; retain it and choose a new profile name"
            )
        self.write_catalog()
        write_private(target, content)

    def probe(self) -> dict[str, Any]:
        """Directory readback only. Never start a thread or submit a model request."""
        expected = self.generate_catalog()
        with tempfile.TemporaryDirectory(prefix="cpa-cli-catalog-probe-") as raw:
            root = Path(raw).resolve()
            home, codex_home = root / "home", root / "codex"
            home.mkdir(mode=0o700)
            codex_home.mkdir(mode=0o700)
            catalog_path = root / "catalog.json"
            write_private(catalog_path, json.dumps(expected))
            profile_name = self.settings.data["profile_name"]
            profile_content = self.profile_content(catalog_path)
            write_private(codex_home / (profile_name + ".config.toml"), profile_content)
            env = {
                "HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "PATH": os.environ.get("PATH", os.defpath),
                "TMPDIR": str(root),
            }
            version = subprocess.run(
                [str(self.CODEX_BINARY), "--version"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            if version.stdout.strip() != "codex-cli " + CODEX_VERSION:
                raise ValueError("CLI version differs from pinned metadata")
            # 0.160.0 only accepts --profile on runtime/debug commands, not
            # app-server. Validate the standalone profile with a no-request
            # renderer, then project the same bounded fields to app-server argv.
            parsed = subprocess.run(
                [
                    str(self.CODEX_BINARY),
                    "--profile",
                    profile_name,
                    "debug",
                    "prompt-input",
                ],
                env=env,
                cwd=home,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            if parsed.returncode != 0:
                raise RuntimeError(
                    "CLI independent profile configuration parsing failed"
                )
            profile = tomllib.loads(profile_content)
            argv = [str(self.CODEX_BINARY)]
            for key in (
                "model",
                "model_provider",
                "model_reasoning_effort",
                "service_tier",
                "model_catalog_json",
            ):
                argv.extend(["-c", key + "=" + json.dumps(profile[key])])
            provider = profile["model_provider"]
            provider_fields = ",".join(
                key + "=" + json.dumps(value)
                for key, value in profile["model_providers"][provider].items()
            )
            argv.extend(
                [
                    "-c",
                    "model_providers={"
                    + json.dumps(provider)
                    + "={"
                    + provider_fields
                    + "}}",
                ]
            )
            argv.append("app-server")
            process = subprocess.Popen(
                argv,
                env=env,
                cwd=home,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            assert process.stdin is not None
            try:
                for message in (
                    {
                        "method": "initialize",
                        "id": 1,
                        "params": {
                            "clientInfo": {
                                "name": "cpa_cli_catalog_probe",
                                "version": "0.2.0",
                            }
                        },
                    },
                ):
                    process.stdin.write(json.dumps(message) + "\n")
                    process.stdin.flush()
                if "error" in read_messages(process, 1, 10):
                    raise RuntimeError("CLI app-server initialization failed")
                process.stdin.write(
                    json.dumps({"method": "initialized", "params": {}}) + "\n"
                )
                process.stdin.write(
                    json.dumps(
                        {
                            "method": "model/list",
                            "id": 2,
                            "params": {"limit": 100, "includeHidden": True},
                        }
                    )
                    + "\n"
                )
                process.stdin.flush()
                response = read_messages(process, 2, 15)
                process.stdin.write(
                    json.dumps(
                        {
                            "method": "config/read",
                            "id": 3,
                            "params": {"includeLayers": False},
                        }
                    )
                    + "\n"
                )
                process.stdin.flush()
                config_response = read_messages(process, 3, 15)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                process.stdin.close()
                assert process.stdout is not None
                process.stdout.close()
        if "error" in response or "error" in config_response:
            raise RuntimeError("CLI app-server configuration/catalog readback failed")
        observed_config = config_response.get("result", {}).get("config", {})
        config_matches = all(
            observed_config.get(key) == profile[key]
            for key in (
                "model",
                "model_provider",
                "model_reasoning_effort",
                "service_tier",
                "model_catalog_json",
            )
        )
        observed_provider = observed_config.get("model_providers", {}).get(provider, {})
        provider_matches = all(
            observed_provider.get(key) == value
            for key, value in profile["model_providers"][provider].items()
        )
        rows = {
            row["id"]: row
            for row in response.get("result", {}).get("data", [])
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        missing, mismatched = [], []
        for expected_row in expected["models"]:
            row = rows.get(expected_row["slug"])
            if row is None:
                missing.append(expected_row["slug"])
            elif (
                row.get("displayName") != expected_row["display_name"]
                or row.get("hidden") != (expected_row["visibility"] != "list")
                or row.get("inputModalities") != expected_row["input_modalities"]
                or row.get("defaultServiceTier")
                != expected_row.get("default_service_tier")
            ):
                mismatched.append(expected_row["slug"])
        unexpected = sorted(set(rows) - {row["slug"] for row in expected["models"]})
        return {
            "schema_version": "codex_cli_catalog_readback_v1",
            "passed": not (missing or mismatched or unexpected)
            and config_matches
            and provider_matches,
            "profile_parsed": True,
            "config_readback_matches": config_matches,
            "provider_readback_matches": provider_matches,
            "model_count": len(rows),
            "missing": sorted(missing),
            "mismatched": sorted(mismatched),
            "unexpected": unexpected,
            "qualification": "catalog_readback_only",
            "online_qualification": "held",
            "source_kind": read_metadata(self.settings.paths["model_metadata"])[
                "source_kind"
            ],
        }
