"""Agent definitions loaded from global and project YAML files.

Agent *roles* are registry state; everything needed to launch an agent lives in
files so it can be personalised per machine and per project:

- global: ``<runtime_root>/agents/*.yaml``
- project: ``<project>/.loopx/agents/*.yaml``

Each file defines one agent (``id`` defaults to the file stem). A project file
overrides the global file with the same id field by field; ``extra_args`` is
replaced as a whole, never concatenated. ``system_prompt_file`` is resolved
relative to the YAML file that set it, so a project can ship its own prompt.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..paths import project_state_path

from ..reasoning_effort import REASONING_EFFORTS
from .codex_config import normalize_codex_config_override
from ._yaml import load_yaml_file
from .errors import AgentConfigError
from .providers import Provider, load_providers

AGENT_ROLES = ("orchestrator", "developer", "acceptor")
AGENT_RUNTIMES = ("claude-code", "codex-cli")
CLAUDE_PERMISSION_MODES = (
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "manual",
    "dontAsk",
    "plan",
)
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_CLAUDE_PERMISSION_MODE = "dontAsk"
DEFAULT_CODEX_SANDBOX = "read-only"
# G12 (design decision 35): the acceptor only reviews, in a throwaway detached
# checkout of the delivered commit, so it runs without a sandbox to build and
# test freely. Explicit per-agent config still wins.
ROLE_DEFAULT_CODEX_SANDBOX = {"acceptor": "danger-full-access"}
ROLE_DEFAULT_CLAUDE_PERMISSION_MODE = {"acceptor": "bypassPermissions"}


def default_codex_sandbox(role: str | None) -> str:
    return ROLE_DEFAULT_CODEX_SANDBOX.get(str(role or ""), DEFAULT_CODEX_SANDBOX)


def default_claude_permission_mode(role: str | None) -> str:
    return ROLE_DEFAULT_CLAUDE_PERMISSION_MODE.get(str(role or ""), DEFAULT_CLAUDE_PERMISSION_MODE)
RUNTIME_PROVIDER_KINDS = {
    "claude-code": ("anthropic",),
    "codex-cli": ("openai", "openai-compatible", "codex-cpa"),
}
PROJECT_AGENTS_DIR = project_state_path(Path(), "agents")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_AGENT_FIELDS = (
    "id",
    "role",
    "runtime",
    "provider",
    "model",
    "reasoning_effort",
    "system_prompt_file",
    "permission_mode",
    "sandbox",
    "max_concurrency",
    "extra_args",
    "enabled",
    "description",
)


@dataclass(frozen=True)
class AgentDefinition:
    id: str
    role: str
    runtime: str
    provider: str
    model: str | None = None
    reasoning_effort: str | None = None
    system_prompt_file: str | None = None
    permission_mode: str | None = None
    sandbox: str | None = None
    max_concurrency: int = 1
    extra_args: tuple[str, ...] = ()
    enabled: bool = True
    description: str | None = None
    sources: tuple[str, ...] = field(default=(), compare=False)
    provider_config: Provider | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "runtime": self.runtime,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "system_prompt_file": self.system_prompt_file,
            "permission_mode": self.permission_mode,
            "sandbox": self.sandbox,
            "max_concurrency": self.max_concurrency,
            "extra_args": list(self.extra_args),
            "enabled": self.enabled,
            "description": self.description,
            "sources": list(self.sources),
            "provider_config": (
                self.provider_config.to_dict() if self.provider_config else None
            ),
        }


def global_agents_dir(runtime_root: Path) -> Path:
    return Path(runtime_root).expanduser() / "agents"


def project_agents_dir(project: Path) -> Path:
    return Path(project).expanduser() / PROJECT_AGENTS_DIR


def _read_layer(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Read one agent file into raw fields with its paths already resolved."""

    try:
        data = load_yaml_file(path)
    except AgentConfigError as exc:
        return None, exc.issues
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        return None, [f"{path}: agent file must be a mapping"]
    issues: list[str] = []
    unknown = sorted(set(map(str, data)) - set(_AGENT_FIELDS))
    if unknown:
        issues.append(f"{path}: unsupported agent fields: {', '.join(unknown)}")
    raw = {key: data[key] for key in _AGENT_FIELDS if key in data}
    raw.setdefault("id", path.stem)
    if raw["id"] != path.stem and "id" in data:
        # An explicit id wins, but a mismatch is almost always a copy/paste
        # mistake that would make the project override miss its target.
        issues.append(f"{path}: id {raw['id']!r} does not match file name {path.stem!r}")
    prompt = raw.get("system_prompt_file")
    if isinstance(prompt, str) and prompt.strip():
        raw["system_prompt_file"] = str(
            (path.parent / Path(prompt.strip()).expanduser()).resolve()
        )
    return raw, issues


def _layer_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        item
        for item in directory.iterdir()
        if item.is_file() and item.suffix in {".yaml", ".yml"}
    )


def _collect(
    runtime_root: Path, project: Path | None
) -> dict[str, tuple[dict[str, Any] | None, list[str], list[str]]]:
    """Merge global then project layers per agent id (field level).

    Returns ``{agent_id: (merged_raw, sources, file_issues)}``. A file that
    cannot be read is attributed to its file stem so it is still reported.
    """

    merged: dict[str, tuple[dict[str, Any] | None, list[str], list[str]]] = {}
    layers = [global_agents_dir(runtime_root)]
    if project is not None:
        layers.append(project_agents_dir(project))
    for directory in layers:
        seen_in_layer: set[str] = set()
        for path in _layer_files(directory):
            raw, layer_issues = _read_layer(path)
            agent_id = str(raw.get("id")) if raw is not None else path.stem
            base, sources, issues = merged.get(agent_id, ({}, [], []))
            issues = [*issues, *layer_issues]
            if agent_id in seen_in_layer:
                issues.append(f"{path}: duplicate agent id {agent_id!r} in {directory}")
            seen_in_layer.add(agent_id)
            if raw is None or base is None:
                merged[agent_id] = (None, [*sources, str(path)], issues)
            else:
                merged[agent_id] = ({**base, **raw}, [*sources, str(path)], issues)
    return merged


def _validate(
    raw: Mapping[str, Any],
    *,
    sources: list[str],
    providers: Mapping[str, Provider] | None,
    registry_role: str | None = None,
) -> tuple[AgentDefinition | None, list[str]]:
    where = sources[-1] if sources else "agent"
    issues: list[str] = []
    agent_id = raw.get("id")
    if not isinstance(agent_id, str) or not _ID_RE.fullmatch(agent_id):
        issues.append(f"{where}: id must match {_ID_RE.pattern}")
    where = f"{where} ({agent_id})"
    role = raw.get("role")
    if role not in AGENT_ROLES:
        issues.append(f"{where}: role must be one of {', '.join(AGENT_ROLES)}")
    runtime = raw.get("runtime")
    if runtime not in AGENT_RUNTIMES:
        issues.append(f"{where}: runtime must be one of {', '.join(AGENT_RUNTIMES)}")
    provider_name = raw.get("provider")
    provider: Provider | None = None
    if not isinstance(provider_name, str) or not provider_name.strip():
        issues.append(f"{where}: provider must name an entry in providers.yaml")
    elif providers is not None:
        provider = providers.get(provider_name)
        if provider is None:
            issues.append(f"{where}: provider {provider_name!r} is not defined in providers.yaml")
        elif runtime in RUNTIME_PROVIDER_KINDS and (
            provider.kind not in RUNTIME_PROVIDER_KINDS[runtime]
        ):
            issues.append(
                f"{where}: runtime {runtime} cannot use provider kind {provider.kind} "
                f"(expected {', '.join(RUNTIME_PROVIDER_KINDS[runtime])})"
            )
    for key in ("model", "description"):
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            issues.append(f"{where}: {key} must be a non-empty string")
    effort = raw.get("reasoning_effort")
    if effort is not None:
        allowed = CLAUDE_EFFORTS if runtime == "claude-code" else REASONING_EFFORTS
        if effort not in allowed:
            issues.append(
                f"{where}: reasoning_effort for {runtime} must be one of {', '.join(allowed)}"
            )
    permission_mode = raw.get("permission_mode")
    sandbox = raw.get("sandbox")
    if runtime == "claude-code":
        if sandbox is not None:
            issues.append(f"{where}: sandbox applies to codex-cli; use permission_mode")
        if permission_mode is None:
            permission_mode = default_claude_permission_mode(registry_role or role)
        elif permission_mode not in CLAUDE_PERMISSION_MODES:
            issues.append(
                f"{where}: permission_mode must be one of {', '.join(CLAUDE_PERMISSION_MODES)}"
            )
    elif runtime == "codex-cli":
        if permission_mode is not None:
            issues.append(f"{where}: permission_mode applies to claude-code; use sandbox")
        if sandbox is None:
            sandbox = default_codex_sandbox(registry_role or role)
        elif sandbox not in CODEX_SANDBOXES:
            issues.append(f"{where}: sandbox must be one of {', '.join(CODEX_SANDBOXES)}")
    prompt = raw.get("system_prompt_file")
    if prompt is not None:
        if not isinstance(prompt, str) or not prompt.strip():
            issues.append(f"{where}: system_prompt_file must be a path string")
        elif not Path(prompt).is_file():
            issues.append(f"{where}: system_prompt_file {prompt} does not exist")
        elif runtime == "codex-cli":
            issues.append(
                f"{where}: system_prompt_file is only supported for claude-code agents"
            )
    max_concurrency = raw.get("max_concurrency", 1)
    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or not 1 <= max_concurrency <= 64
    ):
        issues.append(f"{where}: max_concurrency must be an integer from 1 to 64")
        max_concurrency = 1
    extra_args = raw.get("extra_args", [])
    if extra_args is None:
        extra_args = []
    if not isinstance(extra_args, list) or not all(
        isinstance(item, str) and item and "\x00" not in item for item in extra_args
    ):
        issues.append(f"{where}: extra_args must be a list of non-empty strings")
        extra_args = []
    elif runtime == "codex-cli":
        for item in extra_args:
            try:
                normalize_codex_config_override(item)
            except ValueError:
                issues.append(
                    f"{where}: codex-cli extra_args must be KEY=VALUE config overrides "
                    f"(got {item!r})"
                )
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        issues.append(f"{where}: enabled must be true or false")
        enabled = True
    if issues:
        return None, issues
    return (
        AgentDefinition(
            id=str(agent_id),
            role=str(role),
            runtime=str(runtime),
            provider=str(provider_name),
            model=raw.get("model"),
            reasoning_effort=effort,
            system_prompt_file=prompt,
            permission_mode=permission_mode,
            sandbox=sandbox,
            max_concurrency=max_concurrency,
            extra_args=tuple(extra_args),
            enabled=enabled,
            description=raw.get("description"),
            sources=tuple(sources),
            provider_config=provider,
        ),
        [],
    )


def load_agent_definitions(
    project: Path | None = None,
    *,
    runtime_root: Path,
    providers: Mapping[str, Provider] | None = None,
) -> tuple[dict[str, AgentDefinition], dict[str, list[str]], list[str]]:
    """Load every agent, returning ``(valid, per_agent_issues, provider_issues)``.

    This never raises for a single bad agent so ``agent list``/``validate`` can
    report every problem together. When providers.yaml itself is invalid the
    provider references cannot be checked and every agent is reported invalid.
    """

    provider_issues: list[str] = []
    if providers is None:
        try:
            providers = load_providers(runtime_root)
        except AgentConfigError as exc:
            provider_issues = exc.issues
    valid: dict[str, AgentDefinition] = {}
    invalid: dict[str, list[str]] = {}
    for agent_id, (raw, sources, file_issues) in sorted(
        _collect(runtime_root, project).items()
    ):
        definition, issues = (
            _validate(raw, sources=sources, providers=providers)
            if raw is not None
            else (None, [])
        )
        if provider_issues and definition is not None:
            issues = ["providers.yaml is invalid; provider reference unchecked"]
            definition = None
        if definition is None or file_issues:
            invalid[agent_id] = [*file_issues, *issues]
        else:
            valid[agent_id] = definition
    return valid, invalid, provider_issues


def resolve_agent(
    agent_id: str,
    project: Path | None = None,
    *,
    runtime_root: Path,
    providers: Mapping[str, Provider] | None = None,
    role: str | None = None,
) -> AgentDefinition:
    """Return the merged, validated definition for one agent or raise.

    ``AgentConfigError.issues`` lists every problem found in the agent's files
    and in providers.yaml, each prefixed with the file it concerns. ``role``
    is the agent's registry role when the caller knows it; it selects the
    role-based sandbox and permission defaults (else the file's ``role``).
    """

    if providers is None:
        providers = load_providers(runtime_root)
    merged = _collect(runtime_root, project)
    if agent_id not in merged:
        searched = [str(global_agents_dir(runtime_root))]
        if project is not None:
            searched.append(str(project_agents_dir(project)))
        raise AgentConfigError(
            [f"agent {agent_id!r} is not defined (searched {', '.join(searched)})"]
        )
    raw, sources, file_issues = merged[agent_id]
    definition, issues = (
        _validate(raw, sources=sources, providers=providers, registry_role=role)
        if raw is not None
        else (None, [])
    )
    if definition is None or file_issues:
        raise AgentConfigError([*file_issues, *issues])
    return definition
