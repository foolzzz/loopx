"""Typed configuration errors for agent and provider definition files."""

from __future__ import annotations

from collections.abc import Iterable


class AgentConfigError(ValueError):
    """One or more agent/provider configuration problems.

    ``issues`` holds one human-readable line per problem, each already prefixed
    with the file and field it concerns, so a CLI can list them all at once.
    """

    def __init__(self, issues: Iterable[str]) -> None:
        self.issues = [str(issue) for issue in issues if str(issue)]
        if not self.issues:
            self.issues = ["invalid configuration"]
        super().__init__("; ".join(self.issues))
