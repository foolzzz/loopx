"""Real CLI configuration/profile/catalog readback, without model requests.

Manual opt-in: a local Codex CLI 0.160.0 binary is required. CPA is replaced
by a passive loopback socket that must receive no connection.
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import socket
import sys
import tempfile
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))
CLIModelCatalog = importlib.import_module(
    "loopx_codex_provider_routing.operator_catalog"
).CLIModelCatalog
runtime_module = importlib.import_module(
    "loopx_codex_provider_routing.operator_runtime"
)
CPAOperator, write_private = runtime_module.CPAOperator, runtime_module.write_private
OperatorSettings = importlib.import_module(
    "loopx_codex_provider_routing.operator_settings"
).OperatorSettings
routing_source = importlib.import_module(
    "loopx_codex_provider_routing.selectors"
).routing_source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-binary", default=shutil.which("codex"))
    args = parser.parse_args()
    if not args.codex_binary:
        parser.error("Codex CLI 0.160.0 is required for real configuration readback")
    with (
        tempfile.TemporaryDirectory(prefix="cli-profile-readback-") as raw,
        socket.socket() as listener,
    ):
        root = Path(raw).resolve()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.05)
        source = routing_source()
        source["profiles"] = [
            row for row in source["profiles"] if row["provider"] == "codex"
        ]
        source["routes"] = [
            row for row in source["routes"] if not row.get("fallback_tail")
        ]
        plan = json.loads((PACKAGE_ROOT / "examples/cli-plan.json").read_text())
        plan["cli_plan"]["catalog_source"] = source
        metadata = json.loads(
            (PACKAGE_ROOT / "templates/model-metadata.synthetic.json").read_text()
        )
        write_private(root / "metadata.json", json.dumps(metadata))
        write_private(root / "plan.json", json.dumps(plan))
        settings = OperatorSettings(
            {
                "schema_version": "loopx_cpa_local_operator_v2",
                "paths": {
                    "runtime_root": str(root / "runtime"),
                    "temporary_root": str(root / "temporary"),
                    "binary": str(root / "unused-cpa-binary"),
                    "codex_binary": str(Path(args.codex_binary).resolve()),
                    "model_metadata": str(root / "metadata.json"),
                    "route_plan": str(root / "plan.json"),
                    "codex_home": str(root / "target-codex-home"),
                },
                "binary_sha256": "0" * 64,
                "source_commit": "0" * 40,
                "port": listener.getsockname()[1],
                "launchd_label": "org.example.cli-profile-readback",
                "profile_name": "cpa-native",
                "cpa_client_env_key": "SYNTHETIC_CPA_CLIENT_KEY",
                "fallback_routes": [],
            }
        )
        catalog = CLIModelCatalog(CPAOperator(settings))
        result = catalog.probe()
        assert result["passed"], result
        assert result["profile_parsed"]
        assert result["config_readback_matches"]
        assert result["provider_readback_matches"]
        assert result["model_count"] == 19
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            pass
        else:
            connection.close()
            raise AssertionError("configuration-only probe contacted the CPA endpoint")
        assert not settings.paths["runtime_root"].exists()
        assert not settings.paths["codex_home"].exists()
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
