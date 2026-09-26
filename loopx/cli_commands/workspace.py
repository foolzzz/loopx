"""`loopx workspace`: per-todo git worktrees across a Goal's repos.

prepare/status/merge/cleanup map one-to-one onto
``loopx.workspace.git_workspace``. Nothing here fetches or pushes.
"""

from __future__ import annotations

from ..agent_registry import load_goal_from_registry
from ..workspace import git_workspace

WORKSPACE_ACTIONS = ("prepare", "status", "merge", "cleanup")


def register_workspace(subparsers, add_format):
    parser = subparsers.add_parser(
        "workspace",
        help="Prepare, inspect, atomically merge and clean up a todo's git worktrees across the Goal's repos.",
    )
    actions = parser.add_subparsers(dest="workspace_command", required=True)
    for action, help_text in (
        ("prepare", "Create or reuse one worktree per repo on branch loopx/<goal>/<todo>."),
        ("status", "Per repo: dirty, ahead/behind the merge target, predicted conflicts."),
        ("merge", "Merge the todo branch into every repo's target, all or nothing (never pushes)."),
        ("cleanup", "Remove the todo's worktrees and branches once merged (or with --force)."),
    ):
        sub = actions.add_parser(action, help=help_text)
        add_format(sub)
        sub.add_argument("--goal-id", required=True)
        sub.add_argument("--todo-id", required=True)
        sub.add_argument(
            "--repo",
            dest="repo_names",
            action="append",
            default=None,
            help="Repo name from the Goal's repos list; repeatable. Defaults to every repo.",
        )
        if action != "status":
            sub.add_argument("--dry-run", action="store_true", help="Report the plan without changing anything.")
        if action == "cleanup":
            sub.add_argument(
                "--force",
                action="store_true",
                help="Also remove unmerged branches and dirty worktrees (discards their work).",
            )


def handle_workspace(args, registry_path, runtime_root, print_payload, output_format):
    goal = load_goal_from_registry(registry_path, args.goal_id)
    action = args.workspace_command
    if not goal:
        payload = {
            "ok": False,
            "schema_version": git_workspace.SCHEMA_VERSION,
            "action": action,
            "goal_id": args.goal_id,
            "todo_id": args.todo_id,
            "error_code": "goal_not_registered",
            "reason": "the registry does not know this Goal",
            "repos": [],
        }
        print_payload(payload, output_format(args), render_workspace)
        return 1
    repo_names = args.repo_names or None
    dry_run = bool(getattr(args, "dry_run", False))
    if action == "prepare":
        payload = git_workspace.prepare(goal, args.todo_id, repo_names, runtime_root, dry_run=dry_run)
    elif action == "status":
        payload = git_workspace.status(goal, args.todo_id, repo_names, runtime_root)
    elif action == "merge":
        payload = git_workspace.merge(goal, args.todo_id, repo_names, runtime_root, dry_run=dry_run)
    else:
        payload = git_workspace.cleanup(
            goal, args.todo_id, repo_names, runtime_root, force=bool(args.force), dry_run=dry_run
        )
    print_payload(payload, output_format(args), render_workspace)
    return 0 if payload.get("ok") else 1


def render_workspace(payload):
    head = (
        f"Workspace {payload.get('action')}: goal {payload.get('goal_id')} "
        f"todo {payload.get('todo_id')} branch {payload.get('branch') or '-'}"
        f"{' (dry run)' if payload.get('dry_run') else ''} — "
        f"{'ok' if payload.get('ok') else 'NOT ok'}"
    )
    lines = [head]
    if payload.get("error_code") and not payload.get("repos"):
        lines.append(f"error: {payload.get('error_code')} — {payload.get('reason')}")
        return "\n".join(lines)
    for repo in payload.get("repos") or []:
        lines.append(_render_repo(payload.get("action"), repo))
    if payload.get("action") == "merge":
        if payload.get("merged"):
            lines.append(
                "merged: "
                + ", ".join(f"{item['name']}→{item['target_branch']} {str(item['new'])[:12]}" for item in payload["merged"])
            )
        if payload.get("would_merge"):
            lines.append("would merge: " + ", ".join(payload["would_merge"]))
        report = payload.get("conflict_report")
        if report:
            lines.append(report.get("developer_instruction", ""))
        failure = payload.get("failure")
        if failure:
            lines.append(f"apply failed in {failure.get('name')}: {failure.get('reason')}")
            for item in payload.get("rolled_back") or []:
                lines.append(f"rolled back {item['name']} {item['target_branch']} to {str(item['restored_to'])[:12]}")
            for item in payload.get("rollback_failures") or []:
                lines.append(f"ROLLBACK FAILED {item.get('name')}: {item.get('reason')}")
    lines.append("Nothing is pushed; pushing to a remote is a user gate.")
    return "\n".join(lines)


def _render_repo(action, repo):
    name = repo.get("name")
    if action == "merge":
        text = f"- {name}: {repo.get('state')} → {repo.get('target_branch')}"
        for blocker in repo.get("blockers") or []:
            text += f"\n    blocker {blocker.get('error_code')}: {blocker.get('reason')}"
            paths = blocker.get("conflicted_paths") or blocker.get("paths")
            if paths:
                text += f" [{', '.join(paths)}]"
        return text
    if not repo.get("ok") and repo.get("error_code"):
        return f"- {name}: {repo.get('action') or 'error'} {repo.get('error_code')} — {repo.get('reason')}"
    if action == "status":
        check = (repo.get("merge_check") or {}).get("state", "n/a")
        return (
            f"- {name}: prepared={repo.get('prepared')} dirty={repo.get('dirty', False)} "
            f"ahead={repo.get('ahead', '-')} behind={repo.get('behind', '-')} "
            f"target={repo.get('target_branch')} merge={check} "
            f"blockers={','.join(repo.get('merge_blockers') or []) or 'none'}"
        )
    return f"- {name}: {repo.get('action')} {repo.get('path')}"
