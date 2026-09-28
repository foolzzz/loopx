"""Lazy YAML loading so core LoopX imports never require PyYAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import AgentConfigError


def load_yaml_file(path: Path) -> Any:
    """Parse one YAML file, reporting parse/read problems as config errors."""

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise AgentConfigError(
            [f"{path}: PyYAML is required to read agent/provider config files"]
        ) from exc
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AgentConfigError([f"{path}: invalid YAML: {exc}"]) from exc
    except OSError as exc:
        raise AgentConfigError([f"{path}: unreadable: {exc.strerror or exc}"]) from exc
