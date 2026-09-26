"""Provider definitions loaded from ``<runtime_root>/providers.yaml``.

A provider names *where* a model runs and *how* it authenticates. Secret values
are never stored here: an ``api_key`` provider names an environment variable
and/or a macOS keychain entry, an ``oauth_token`` provider names the
environment variable carrying the token, and an ``oauth_cli`` provider defers
to the CLI's own login. Literal secret-looking fields are rejected on load.

File shape (either a list or a mapping keyed by name)::

    providers:
      - name: anthropic-login
        kind: anthropic
        auth: {type: oauth_cli}
      - name: cpa
        kind: codex-cpa
        auth: {type: api_key, env: CPA_API_KEY}
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import AgentConfigError

PROVIDERS_FILENAME = "providers.yaml"
PROVIDER_KINDS = ("anthropic", "openai", "openai-compatible", "codex-cpa")
AUTH_TYPES = ("api_key", "oauth_cli", "oauth_token")
OAUTH_CLIS = ("claude", "codex")
WIRE_APIS = ("responses", "chat")
CPA_DEFAULT_BASE_URL = "http://127.0.0.1:8317/v1"
CPA_DEFAULT_ENV_KEY = "CPA_API_KEY"

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_TOML_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_PROVIDER_FIELDS = {
    "name",
    "kind",
    "auth",
    "base_url",
    "wire_api",
    "model_provider_id",
    "display_name",
    "service_tier",
    "description",
}
_AUTH_FIELDS = {"type", "env", "keychain", "cli"}
_KEYCHAIN_FIELDS = {"service", "account"}
# Field names that would carry a secret value. They are refused outright so a
# credential can never be committed to (or echoed from) a config file.
_SECRET_FIELD_NAMES = {
    "api_key",
    "apikey",
    "key",
    "token",
    "secret",
    "password",
    "value",
    "oauth_token",
    "access_token",
    "refresh_token",
}


@dataclass(frozen=True)
class KeychainRef:
    service: str
    account: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"service": self.service, "account": self.account}


@dataclass(frozen=True)
class ProviderAuth:
    type: str
    env: str | None = None
    keychain: KeychainRef | None = None
    cli: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "env": self.env,
            "keychain": self.keychain.to_dict() if self.keychain else None,
            "cli": self.cli,
        }


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str
    auth: ProviderAuth
    base_url: str | None = None
    wire_api: str | None = None
    model_provider_id: str | None = None
    display_name: str | None = None
    service_tier: str | None = None
    description: str | None = None
    source: str | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "auth": self.auth.to_dict(),
            "base_url": self.base_url,
            "wire_api": self.wire_api,
            "model_provider_id": self.model_provider_id,
            "display_name": self.display_name,
            "service_tier": self.service_tier,
            "description": self.description,
            "source": self.source,
        }


def providers_path(runtime_root: Path) -> Path:
    return Path(runtime_root).expanduser() / PROVIDERS_FILENAME


def _optional_str(
    raw: Mapping[str, Any], key: str, where: str, issues: list[str]
) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{where}: {key} must be a non-empty string")
        return None
    return value.strip()


def _parse_auth(
    raw: Any, *, kind: str, where: str, issues: list[str]
) -> ProviderAuth | None:
    if not isinstance(raw, Mapping):
        issues.append(f"{where}: auth must be a mapping with a type")
        return None
    secret_fields = sorted(
        key for key in raw if str(key).lower() in _SECRET_FIELD_NAMES
    )
    if secret_fields:
        issues.append(
            f"{where}: auth.{secret_fields[0]} looks like a secret value; "
            "reference an env var (auth.env) or keychain entry (auth.keychain) instead"
        )
        return None
    unknown = sorted(set(map(str, raw)) - _AUTH_FIELDS)
    if unknown:
        issues.append(f"{where}: unsupported auth fields: {', '.join(unknown)}")
    auth_type = raw.get("type")
    if auth_type not in AUTH_TYPES:
        issues.append(
            f"{where}: auth.type must be one of {', '.join(AUTH_TYPES)}"
        )
        return None
    env = raw.get("env")
    if env is not None and (not isinstance(env, str) or not _ENV_RE.fullmatch(env)):
        issues.append(f"{where}: auth.env must be an environment variable name")
        env = None
    keychain: KeychainRef | None = None
    keychain_raw = raw.get("keychain")
    if keychain_raw is not None:
        if isinstance(keychain_raw, str):
            keychain_raw = {"service": keychain_raw}
        if not isinstance(keychain_raw, Mapping):
            issues.append(f"{where}: auth.keychain must be a service name or mapping")
        else:
            unknown_kc = sorted(set(map(str, keychain_raw)) - _KEYCHAIN_FIELDS)
            if unknown_kc:
                issues.append(
                    f"{where}: unsupported auth.keychain fields: {', '.join(unknown_kc)}"
                )
            service = keychain_raw.get("service")
            account = keychain_raw.get("account")
            if not isinstance(service, str) or not service.strip():
                issues.append(f"{where}: auth.keychain.service must be a non-empty string")
            elif account is not None and (
                not isinstance(account, str) or not account.strip()
            ):
                issues.append(f"{where}: auth.keychain.account must be a non-empty string")
            else:
                keychain = KeychainRef(
                    service=service.strip(),
                    account=account.strip() if isinstance(account, str) else None,
                )
    cli = raw.get("cli")
    if auth_type == "api_key":
        if env is None and keychain is None and kind != "codex-cpa":
            issues.append(
                f"{where}: api_key auth needs auth.env and/or auth.keychain"
            )
        if kind == "codex-cpa" and env is None:
            env = CPA_DEFAULT_ENV_KEY
        if cli is not None:
            issues.append(f"{where}: auth.cli only applies to oauth_cli")
        return ProviderAuth(type="api_key", env=env, keychain=keychain)
    if auth_type == "oauth_token":
        if env is None:
            issues.append(
                f"{where}: oauth_token auth needs auth.env "
                "(for example CLAUDE_CODE_OAUTH_TOKEN)"
            )
        if keychain is not None or cli is not None:
            issues.append(f"{where}: oauth_token auth only accepts auth.env")
        return ProviderAuth(type="oauth_token", env=env)
    # oauth_cli
    if cli is None:
        cli = "claude" if kind == "anthropic" else "codex"
    if cli not in OAUTH_CLIS:
        issues.append(f"{where}: auth.cli must be one of {', '.join(OAUTH_CLIS)}")
        return None
    if env is not None or keychain is not None:
        issues.append(f"{where}: oauth_cli auth uses the CLI login; drop auth.env/keychain")
    return ProviderAuth(type="oauth_cli", cli=cli)


def parse_provider(
    raw: Any, *, where: str, default_name: str | None = None
) -> tuple[Provider | None, list[str]]:
    issues: list[str] = []
    if not isinstance(raw, Mapping):
        return None, [f"{where}: provider entry must be a mapping"]
    secret_fields = sorted(
        key for key in raw if str(key).lower() in _SECRET_FIELD_NAMES
    )
    if secret_fields:
        return None, [
            f"{where}: {secret_fields[0]} looks like a secret value; "
            "providers.yaml may only reference env vars or keychain entries"
        ]
    unknown = sorted(set(map(str, raw)) - _PROVIDER_FIELDS)
    if unknown:
        issues.append(f"{where}: unsupported provider fields: {', '.join(unknown)}")
    name = raw.get("name", default_name)
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        issues.append(f"{where}: name must match {_NAME_RE.pattern}")
        name = None
    where = f"{where} ({name})" if name else where
    kind = raw.get("kind")
    if kind not in PROVIDER_KINDS:
        issues.append(f"{where}: kind must be one of {', '.join(PROVIDER_KINDS)}")
        return None, issues
    auth = _parse_auth(raw.get("auth"), kind=kind, where=where, issues=issues)
    base_url = _optional_str(raw, "base_url", where, issues)
    if base_url is not None and not re.match(r"^https?://[^\s]+$", base_url):
        issues.append(f"{where}: base_url must be an http(s) URL")
    wire_api = _optional_str(raw, "wire_api", where, issues)
    if wire_api is not None and wire_api not in WIRE_APIS:
        issues.append(f"{where}: wire_api must be one of {', '.join(WIRE_APIS)}")
    model_provider_id = _optional_str(raw, "model_provider_id", where, issues)
    if model_provider_id is not None and not _TOML_KEY_RE.fullmatch(model_provider_id):
        issues.append(f"{where}: model_provider_id must match {_TOML_KEY_RE.pattern}")
    display_name = _optional_str(raw, "display_name", where, issues)
    service_tier = _optional_str(raw, "service_tier", where, issues)
    description = _optional_str(raw, "description", where, issues)
    codex_only = {
        "wire_api": wire_api,
        "model_provider_id": model_provider_id,
        "display_name": display_name,
        "service_tier": service_tier,
    }
    if kind == "anthropic":
        for key, value in codex_only.items():
            if value is not None:
                issues.append(f"{where}: {key} does not apply to kind anthropic")
    if kind == "openai-compatible" and base_url is None:
        issues.append(f"{where}: openai-compatible providers need base_url")
    if kind == "codex-cpa":
        base_url = base_url or CPA_DEFAULT_BASE_URL
        wire_api = wire_api or "responses"
        model_provider_id = model_provider_id or "cpa"
        display_name = display_name or "CPA"
        service_tier = service_tier or "default"
        if auth is not None and auth.type != "api_key":
            issues.append(f"{where}: codex-cpa providers use api_key auth (auth.env)")
    if kind == "openai-compatible":
        wire_api = wire_api or "responses"
        model_provider_id = model_provider_id or (
            re.sub(r"[^A-Za-z0-9_-]", "_", name) if name else None
        )
        display_name = display_name or name
        if auth is not None and auth.type == "oauth_cli":
            issues.append(f"{where}: openai-compatible providers cannot use oauth_cli")
    if issues or auth is None or name is None:
        return None, issues or [f"{where}: invalid provider"]
    return (
        Provider(
            name=name,
            kind=kind,
            auth=auth,
            base_url=base_url,
            wire_api=wire_api,
            model_provider_id=model_provider_id,
            display_name=display_name,
            service_tier=service_tier,
            description=description,
        ),
        [],
    )


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AgentConfigError([f"{path}: invalid YAML: {exc}"]) from exc
    except OSError as exc:
        raise AgentConfigError([f"{path}: unreadable: {exc.strerror or exc}"]) from exc


def load_providers(runtime_root: Path) -> dict[str, Provider]:
    """Load and validate every provider; raise one error listing all issues.

    A missing file means "no providers configured" and returns ``{}``.
    """

    path = providers_path(runtime_root)
    if not path.exists():
        return {}
    data = _load_yaml(path)
    if data is None:
        return {}
    if not isinstance(data, Mapping) or "providers" not in data:
        raise AgentConfigError([f"{path}: expected a top-level `providers` key"])
    unknown = sorted(set(map(str, data)) - {"providers", "schema_version"})
    issues: list[str] = []
    if unknown:
        issues.append(f"{path}: unsupported top-level keys: {', '.join(unknown)}")
    entries = data.get("providers") or []
    items: list[tuple[str, Any, str | None]]
    if isinstance(entries, Mapping):
        items = [
            (f"{path}: providers.{key}", value, str(key))
            for key, value in entries.items()
        ]
        for key, value in entries.items():
            if isinstance(value, Mapping) and "name" in value and value["name"] != key:
                issues.append(f"{path}: providers.{key}: name does not match its key")
    elif isinstance(entries, list):
        items = [
            (f"{path}: providers[{index}]", value, None)
            for index, value in enumerate(entries)
        ]
    else:
        raise AgentConfigError([f"{path}: providers must be a list or mapping"])
    providers: dict[str, Provider] = {}
    for where, raw, default_name in items:
        provider, provider_issues = parse_provider(
            raw, where=where, default_name=default_name
        )
        issues.extend(provider_issues)
        if provider is None:
            continue
        if provider.name in providers:
            issues.append(f"{where}: duplicate provider name {provider.name!r}")
            continue
        providers[provider.name] = Provider(
            **{**provider.__dict__, "source": str(path)}
        )
    if issues:
        raise AgentConfigError(issues)
    return providers
