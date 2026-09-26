"""Validated Codex ``-c KEY=VALUE`` configuration overrides."""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = ["codex_config_arguments", "normalize_codex_config_override"]

# Codex `-c` keys are dotted TOML paths. Keep them to plain segments so a key
# can never smuggle a second flag or a newline into the host argv.
_CODEX_CONFIG_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*(\.[A-Za-z0-9_][A-Za-z0-9_-]*)*$")
_CODEX_CONFIG_VALUE_MAX = 4096


def normalize_codex_config_override(value: str) -> str:
    """Validate one ``KEY=VALUE`` Codex config override and return it."""

    if not isinstance(value, str) or "=" not in value:
        raise ValueError("--codex-config must be KEY=VALUE")
    key, _, raw = value.partition("=")
    key = key.strip()
    if not _CODEX_CONFIG_KEY_RE.fullmatch(key):
        raise ValueError(f"--codex-config key {key!r} must be a dotted config path")
    if any(character in raw for character in ("\x00", "\r", "\n")):
        raise ValueError("--codex-config value must be a single line")
    if not raw or len(raw) > _CODEX_CONFIG_VALUE_MAX:
        raise ValueError("--codex-config value must be 1-4096 characters")
    return f"{key}={raw}"


def codex_config_arguments(overrides: Iterable[str] | None) -> list[str]:
    """Return ``["-c", "K=V", ...]`` for validated overrides, in order."""

    argv: list[str] = []
    for item in overrides or ():
        argv.extend(["-c", normalize_codex_config_override(item)])
    return argv
