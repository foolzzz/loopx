"""Private, explicit local targets for the opt-in CPA operator CLI."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit


def reject_symlinks(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("operator paths must not traverse symbolic links")


class OperatorSettings:
    PATH_KEYS: ClassVar[set[str]] = {
        "runtime_root",
        "temporary_root",
        "binary",
        "codex_binary",
        "model_metadata",
        "route_plan",
    }
    OPTIONAL_PATH_KEYS: ClassVar[set[str]] = {
        "login_source",
        "plugin_directory",
        "ark_env_file",
        "codex_home",
    }
    VALUE_KEYS: ClassVar[set[str]] = {
        "schema_version",
        "paths",
        "binary_sha256",
        "source_commit",
        "port",
        "launchd_label",
        "profile_name",
        "cpa_client_env_key",
        "fallback_routes",
    }
    OPTIONAL_VALUE_KEYS: ClassVar[set[str]] = {
        "plugin_sha256",
        "ark_base_url",
        "ark_model",
        "ark_pro_model",
    }

    def __init__(self, data: dict):
        if (
            not isinstance(data, dict)
            or not self.VALUE_KEYS <= data.keys()
            or set(data) - self.VALUE_KEYS - self.OPTIONAL_VALUE_KEYS
            or data["schema_version"] != "loopx_cpa_local_operator_v2"
        ):
            raise ValueError("local configuration has missing or unsupported fields")
        paths = data["paths"]
        if (
            not isinstance(paths, dict)
            or not self.PATH_KEYS <= paths.keys()
            or set(paths) - self.PATH_KEYS - self.OPTIONAL_PATH_KEYS
        ):
            raise ValueError("local configuration requires explicit path references")
        self.paths = {}
        for key, raw in paths.items():
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                raise ValueError(f"{key} requires an absolute local path")
            reject_symlinks(Path(raw))
            self.paths[key] = Path(raw).resolve()
        if "codex_home" in self.paths:
            target = self.paths["codex_home"]
            if (
                target == Path(target.anchor)
                or target == Path.home()
                or any(
                    part.endswith(".app") or part in {"sessions", "automations"}
                    for part in target.parts
                )
            ):
                raise ValueError("profile target must be a dedicated Codex home")
        protected = {Path.home() / ".codex"}
        if os.environ.get("CODEX_HOME"):
            protected.add(Path(os.environ["CODEX_HOME"]).resolve())
        if "codex_home" in self.paths:
            protected.add(self.paths["codex_home"])
        for key in ("runtime_root", "temporary_root"):
            target = self.paths[key]
            if target == Path(target.anchor) or target == Path.home():
                raise ValueError(f"{key} must be a dedicated directory")
            if any((parent / ".git").exists() for parent in (target, *target.parents)):
                raise ValueError(f"{key} must be outside Git worktrees")
            if any(
                target == path
                or target.is_relative_to(path)
                or path.is_relative_to(target)
                for path in protected
            ):
                raise ValueError("operator runtime must be separate from Codex homes")
            if any(
                part in {".codex", ".app", "automations", "sessions"}
                or part.endswith(".app")
                for part in target.parts
            ):
                raise ValueError(
                    "operator runtime must not contain host stores or bundles"
                )
        root, temporary = (
            self.paths[key] for key in ("runtime_root", "temporary_root")
        )
        if (
            root == temporary
            or root.is_relative_to(temporary)
            or temporary.is_relative_to(root)
        ):
            raise ValueError("runtime and temporary roots must be disjoint")
        for key in ("binary_sha256", "plugin_sha256"):
            if key in data and (
                not isinstance(data[key], str)
                or re.fullmatch(r"[0-9a-f]{64}", data[key]) is None
            ):
                raise ValueError(f"{key} must be an exact SHA-256")
        if (
            not isinstance(data["source_commit"], str)
            or re.fullmatch(r"[0-9a-f]{40}", data["source_commit"]) is None
        ):
            raise ValueError("source_commit must be an exact Git commit")
        if type(data["port"]) is not int or not 1024 <= data["port"] <= 65535:
            raise ValueError("port must be an unprivileged TCP port")
        for key in ("launchd_label", "profile_name"):
            if (
                not isinstance(data[key], str)
                or re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
                    if key == "profile_name"
                    else r"[A-Za-z0-9._-]+",
                    data[key],
                )
                is None
            ):
                raise ValueError(f"{key} must be a symbolic identifier")
        if data["profile_name"] in {"config", "auth"}:
            raise ValueError("profile_name must identify an independent profile")
        if (
            not isinstance(data["cpa_client_env_key"], str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", data["cpa_client_env_key"]) is None
        ):
            raise ValueError("cpa_client_env_key must be an environment variable name")
        from .selectors import ROUTES

        fallbacks = data["fallback_routes"]
        if (
            not isinstance(fallbacks, list)
            or any(
                not isinstance(route, str)
                or route not in ROUTES
                or not ROUTES[route]["tail"]
                for route in fallbacks
            )
            or len(set(fallbacks)) != len(fallbacks)
        ):
            raise ValueError("fallback_routes requires explicit supported selectors")
        if fallbacks:
            if (
                not {"ark_base_url", "ark_model", "ark_pro_model"} <= data.keys()
                or "ark_env_file" not in paths
            ):
                raise ValueError("active fallback routes require explicit Ark settings")
        if "ark_base_url" in data:
            url = urlsplit(data["ark_base_url"])
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("fallback endpoint must be credential-free HTTPS")
        for key in ("ark_model", "ark_pro_model"):
            if key in data and (
                not isinstance(data[key], str)
                or re.fullmatch(r"[a-z0-9._-]+", data[key]) is None
            ):
                raise ValueError(f"{key} must be a model identifier")
        if ("plugin_directory" in paths) != ("plugin_sha256" in data):
            raise ValueError("plugin directory and digest must be supplied together")
        self.data = data

    @classmethod
    def read(cls, path: Path):
        reject_symlinks(path)
        if not path.is_absolute() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError(
                "operator config requires a private regular file (mode 0600)"
            )
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def runtime_attributes(self):
        p, d = self.paths, self.data
        root, temporary = p["runtime_root"], p["temporary_root"]
        state, logs = root / "state", root / "logs"
        return {
            "SOURCE_COMMIT": d["source_commit"],
            "BINARY_SHA256": d["binary_sha256"],
            "BINARY": p["binary"],
            "PLUGIN_DIR": p.get("plugin_directory"),
            "FAST_SELECTOR_PLUGIN": p["plugin_directory"] / "fast-selector-tier.dylib"
            if "plugin_directory" in p
            else None,
            "FAST_SELECTOR_PLUGIN_SHA256": d.get("plugin_sha256"),
            "RUNTIME_ROOT": root,
            "AUTH_DIR": root / "auth",
            "STATE_DIR": state,
            "LOG_DIR": logs,
            "MODEL_CATALOG": root / "codex-model-catalog.json",
            "PROFILE_DIR": root / "profiles",
            "PROFILE_FILE": root / "profiles" / (d["profile_name"] + ".config.toml"),
            "INSTALLED_PROFILE": p["codex_home"] / (d["profile_name"] + ".config.toml")
            if "codex_home" in p
            else None,
            "PID_FILE": state / "cpa.pid",
            "SLOTS_FILE": state / "oauth-slots.json",
            "MANAGEMENT_KEY_FILE": state / "management.key",
            "STATUS_SNAPSHOT_FILE": state / "route-status.json",
            "RUNTIME_TMP_ROOT": temporary,
            "RUNTIME_CONFIG": temporary / "runtime-config.yaml",
            "PORT": d["port"],
            "LAUNCHD_LABEL": d["launchd_label"],
            "ARK_BASE_URL": d.get("ark_base_url"),
            "ARK_MODEL": d.get("ark_model"),
            "ARK_PRO_MODEL": d.get("ark_pro_model"),
            "ARK_LEGACY_MODELS": ("deepseek-v4-flash", d.get("ark_model")),
            "LOG_CANDIDATES": (logs / "launchd.log", logs / "cpa.log"),
        }
