"""The named repo list of one Goal.

A Goal declares ``repos: [{name, path, default_branch, merge_target,
task_branch}]`` in its registry entry. Older Goals only carry a single
``repo`` path; the reader projects that as one repo named ``main`` so callers
never branch on the legacy shape.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MERGE_TARGETS = ("main", "task_branch")
LEGACY_REPO_NAME = "main"
_SPEC_KEYS = ("default_branch", "merge_target", "task_branch")


def _clean_branch(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if (
        text.startswith(("-", "/"))
        or text.endswith(("/", ".lock", "."))
        or ".." in text
        or "//" in text
        or "@{" in text
        or any(ch in text for ch in " ~^:?*[\\\t\n")
    ):
        raise ValueError(f"{field} is not a valid git branch name: {text!r}")
    return text


def normalize_repo_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one repo entry and return its canonical registry shape."""

    if not isinstance(entry, Mapping):
        raise ValueError("repo entry must be an object")
    name = str(entry.get("name") or "").strip()
    if not REPO_NAME_PATTERN.match(name):
        raise ValueError(
            f"repo name must match {REPO_NAME_PATTERN.pattern}: {name!r}"
        )
    raw_path = str(entry.get("path") or "").strip()
    if not raw_path:
        raise ValueError(f"repo {name}: path is required")
    path = os.path.expanduser(raw_path)
    if not os.path.isabs(path):
        raise ValueError(f"repo {name}: path must be absolute: {raw_path!r}")
    merge_target = str(entry.get("merge_target") or "main").strip()
    if merge_target not in MERGE_TARGETS:
        raise ValueError(
            f"repo {name}: merge_target must be one of {', '.join(MERGE_TARGETS)}"
        )
    task_branch = _clean_branch(entry.get("task_branch"), field=f"repo {name}: task_branch")
    if task_branch and merge_target != "task_branch":
        raise ValueError(f"repo {name}: task_branch requires merge_target=task_branch")
    normalized: dict[str, Any] = {
        "name": name,
        "path": os.path.normpath(path),
        "default_branch": _clean_branch(
            entry.get("default_branch"), field=f"repo {name}: default_branch"
        ),
        "merge_target": merge_target,
    }
    if task_branch:
        normalized["task_branch"] = task_branch
    return normalized


def parse_repo_spec(spec: str) -> dict[str, Any]:
    """Parse ``name=<path>[,default_branch=..][,merge_target=..][,task_branch=..]``."""

    text = str(spec or "").strip()
    head, _, rest = text.partition(",")
    name, separator, path = head.partition("=")
    if not separator:
        raise ValueError(
            "--repo must use NAME=/abs/path[,default_branch=B][,merge_target=main|task_branch][,task_branch=B]"
        )
    entry: dict[str, Any] = {"name": name.strip(), "path": path.strip()}
    for part in [item for item in rest.split(",") if item.strip()] if rest else []:
        key, eq, value = part.partition("=")
        key = key.strip()
        if not eq or key not in _SPEC_KEYS:
            raise ValueError(
                f"--repo option {part.strip()!r} must be one of {', '.join(k + '=' for k in _SPEC_KEYS)}"
            )
        if key in entry:
            raise ValueError(f"--repo option {key} given twice")
        entry[key] = value.strip()
    return normalize_repo_entry(entry)


def merge_goal_repos(
    existing: Iterable[Mapping[str, Any]] | None,
    updates: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Upsert repo entries by name, keeping declaration order."""

    merged: dict[str, dict[str, Any]] = {}
    for entry in existing or []:
        if isinstance(entry, Mapping):
            normalized = normalize_repo_entry(entry)
            merged[normalized["name"]] = normalized
    seen: set[str] = set()
    for entry in updates:
        normalized = normalize_repo_entry(entry)
        if normalized["name"] in seen:
            raise ValueError(f"duplicate --repo for {normalized['name']}")
        seen.add(normalized["name"])
        merged[normalized["name"]] = normalized
    return list(merged.values())


def declared_goal_repos(goal: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The explicit ``repos`` list only (no legacy fallback)."""

    raw = (goal or {}).get("repos")
    if not isinstance(raw, list):
        return []
    return [normalize_repo_entry(entry) for entry in raw if isinstance(entry, Mapping)]


def goal_repos(goal: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Every repo of a Goal, falling back to the legacy single ``repo`` path."""

    declared = declared_goal_repos(goal)
    if declared:
        return declared
    legacy = str((goal or {}).get("repo") or "").strip()
    if not legacy:
        return []
    path = Path(os.path.expanduser(legacy))
    return [
        {
            "name": LEGACY_REPO_NAME,
            "path": os.path.normpath(str(path if path.is_absolute() else path.resolve())),
            "default_branch": None,
            "merge_target": "main",
            "legacy": True,
        }
    ]
