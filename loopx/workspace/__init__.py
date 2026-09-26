"""Git multi-repo workspaces for development todos (fork design decisions 18, 21, 22)."""

from .repos import goal_repos, merge_goal_repos, parse_repo_spec

__all__ = ["goal_repos", "merge_goal_repos", "parse_repo_spec"]
