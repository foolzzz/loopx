from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable


@dataclass(frozen=True)
class ExcludedPathTree:
    """One repository tree excluded from active-contract canary selection."""

    root: PurePosixPath
    reason: str

    def contains(self, candidate: str | PurePosixPath) -> bool:
        path = PurePosixPath(candidate)
        return path == self.root or self.root in path.parents


# benchmark/README.md registers this tree as an immutable experiment snapshot.
# Its own identity checker remains authoritative; active-contract canaries must
# not reinterpret the historical source as current product guidance.
ACTIVE_CANARY_EXCLUDED_PATH_TREES: tuple[ExcludedPathTree, ...] = (
    ExcludedPathTree(
        root=PurePosixPath("benchmark/deepswe-gptxhigh-v1"),
        reason="immutable experiment snapshot",
    ),
)


def is_excluded_from_active_canary_scans(
    candidate: str | PurePosixPath,
) -> bool:
    return any(tree.contains(candidate) for tree in ACTIVE_CANARY_EXCLUDED_PATH_TREES)


def partition_active_canary_paths(paths: Iterable[str]) -> tuple[list[str], list[str]]:
    active: list[str] = []
    excluded: list[str] = []
    for path in paths:
        (excluded if is_excluded_from_active_canary_scans(path) else active).append(path)
    return active, excluded
