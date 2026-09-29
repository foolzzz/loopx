from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from hashlib import sha256
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from ...repository_identity import (
    normalize_repository_identity,
    resolve_project_identity,
)
from ..todos.contract import (
    normalize_todo_claimed_by,
    normalize_todo_task_repositories,
    normalize_todo_task_repository,
)
from .delivery_workspace import (
    LOCAL_REPOSITORY_ID_PREFIX,
    build_delivery_workspace_snapshot,
    normalize_delivery_workspace_snapshot,
    todo_workspace_identity_ref,
)

AGENT_WORKSPACE_GUARD_SCHEMA_VERSION = "agent_workspace_guard_v1"
# Workspace kinds that satisfy a peer independent-worktree requirement: a
# linked git worktree, or a verified fork S5 per-Todo workspace root whose
# repos are all linked worktrees on the Todo branch.
INDEPENDENT_DELIVERY_WORKSPACE_KINDS = frozenset(
    {"independent_git_worktree", "todo_workspace_root"}
)


class PeerDeliveryWorkspace(str, Enum):
    """Where one peer's accountable delivery must be refreshed from."""

    # A single-agent Goal, a policy that turns the guard off, or the role_v1
    # orchestrator on a Todo without repositories: it plans from the Goal's
    # state home and delivers no code.
    ANY = "any"
    # An independent git worktree or the Todo's verified per-Todo workspace root.
    INDEPENDENT_WORKTREE = "independent_worktree"
    # A role_v1 developer or acceptor Todo that names no repository has no
    # per-Todo workspace by design: it is delivered from the Goal's project
    # directory. An independent worktree still satisfies it.
    GOAL_PROJECT_DIRECTORY = "goal_project_directory"


def todo_names_repository(todo: Mapping[str, Any]) -> bool:
    """Whether a Todo targets a repository (``task_repositories`` or ``task_repository``)."""

    return bool(
        normalize_todo_task_repositories(todo.get("task_repositories"))
        or normalize_todo_task_repository(todo.get("task_repository"))
    )


def todo_sources_view(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """One view of a Todo for ``peer_delivery_workspace`` from all its persisted records.

    Records that disagree about the repositories give ``None``, an unknown
    Todo, for which the rule stays strict: a stale source without repository
    fields never relaxes a Todo that another source says names a repository.
    """

    repositories = {
        (
            tuple(normalize_todo_task_repositories(record.get("task_repositories"))),
            normalize_todo_task_repository(record.get("task_repository")),
        )
        for record in records
    }
    return records[0] if records and len(repositories) == 1 else None


def goal_project_directory(goal: Mapping[str, Any] | None) -> Path | None:
    """The Goal's registered project directory (its ``repo``), never a caller's ``--project``."""

    raw = str((goal or {}).get("repo") or "").strip()
    path = Path(raw).expanduser() if raw else None
    return path if path is not None and path.is_absolute() else None


def peer_delivery_workspace(
    goal: Mapping[str, Any] | None,
    *,
    agent_id: str | None,
    multi_agent: bool,
    todo: Mapping[str, Any] | None,
) -> PeerDeliveryWorkspace:
    """Decide the workspace an accountable delivery of ``todo`` by ``agent_id`` needs.

    ``workspace_guard_policy.peer_independent_worktree_required`` set to
    ``true`` keeps every peer of a multi-agent Goal on independent worktrees,
    and any other explicit value turns the guard off. Without it, on a role_v1
    Goal and a Todo that names no repository, the orchestrator is exempt and
    a developer or acceptor delivers from the Goal's project directory. A
    Todo that names a repository, or an unknown one, keeps the independent
    worktree for every role.
    """

    goal = goal if isinstance(goal, Mapping) else {}
    policy = goal.get("workspace_guard_policy")
    explicit = policy.get("peer_independent_worktree_required") if isinstance(policy, Mapping) else None
    if not multi_agent or (explicit is not None and explicit is not True):
        return PeerDeliveryWorkspace.ANY
    if explicit is True:
        return PeerDeliveryWorkspace.INDEPENDENT_WORKTREE
    from ...agent_registry import agent_role_for_goal
    from .runtime_model import (
        AGENT_ROLE_ACCEPTOR,
        AGENT_ROLE_DEVELOPER,
        AGENT_ROLE_ORCHESTRATOR,
        AgentRuntimeModel,
        agent_runtime_model_for_goal,
    )

    try:
        role_v1 = agent_runtime_model_for_goal(goal) is AgentRuntimeModel.ROLE_V1
    except ValueError:
        role_v1 = False
    role = agent_role_for_goal(dict(goal), agent_id) if role_v1 else None
    if not isinstance(todo, Mapping) or todo_names_repository(todo):
        return PeerDeliveryWorkspace.INDEPENDENT_WORKTREE
    if role == AGENT_ROLE_ORCHESTRATOR:
        return PeerDeliveryWorkspace.ANY
    if role in {AGENT_ROLE_DEVELOPER, AGENT_ROLE_ACCEPTOR}:
        return PeerDeliveryWorkspace.GOAL_PROJECT_DIRECTORY
    return PeerDeliveryWorkspace.INDEPENDENT_WORKTREE


def delivery_workspace_satisfies(
    requirement: PeerDeliveryWorkspace,
    snapshot: Mapping[str, Any] | None,
    *,
    delivery_path: Path,
    project_directory: Path | None,
) -> bool:
    """Whether a captured delivery ``snapshot`` from ``delivery_path`` meets ``requirement``."""

    if requirement is PeerDeliveryWorkspace.ANY:
        return True
    if snapshot is None:
        return False
    if snapshot.get("workspace_kind") in INDEPENDENT_DELIVERY_WORKSPACE_KINDS:
        return True
    return (
        requirement is PeerDeliveryWorkspace.GOAL_PROJECT_DIRECTORY
        and project_directory is not None
        and _is_same_or_child_path(delivery_path, project_directory)
    )


PEER_WRITE_ACTION_KINDS = {
    "fix",
    "implement",
    "rebuild",
    "repair",
    "writeback",
}


def _is_same_or_child_path(path: Path, root: Path) -> bool:
    try:
        current = path.expanduser().resolve()
        target = root.expanduser().resolve()
    except OSError:
        return False
    return current == target or target in current.parents


def _git_command_output(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _git_worktree_root(path: Path) -> Path | None:
    output = _git_command_output(path, "rev-parse", "--show-toplevel")
    if not output:
        return None
    try:
        return Path(output).expanduser().resolve()
    except OSError:
        return None


def _git_common_dir(path: Path) -> Path | None:
    root = _git_worktree_root(path)
    if root is None:
        return None
    output = _git_command_output(root, "rev-parse", "--git-common-dir")
    if not output:
        return None
    common = Path(output).expanduser()
    if not common.is_absolute():
        common = root / common
    try:
        return common.resolve()
    except OSError:
        return None


def _git_dir(path: Path) -> Path | None:
    root = _git_worktree_root(path)
    if root is None:
        return None
    output = _git_command_output(root, "rev-parse", "--git-dir")
    if not output:
        return None
    git_dir = Path(output).expanduser()
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    try:
        return git_dir.resolve()
    except OSError:
        return None


def _local_repository_identity(path: Path) -> str | None:
    """A stable identity for a repository without a usable ``origin``.

    ``local:`` + sha256 of the realpath of ``git rev-parse --git-common-dir``,
    so a checkout and all of its linked worktrees share it, and no local path
    is recorded.
    """

    common = _git_common_dir(path)
    if common is None:
        return None
    digest = sha256(os.path.realpath(str(common)).encode("utf-8")).hexdigest()
    return f"{LOCAL_REPOSITORY_ID_PREFIX}{digest}"


def _git_repository_identity(path: Path) -> str | None:
    """The delivery ``repo_id``: the canonical origin, else a local identity.

    A local-only repository (no ``origin``, or one that does not normalize to
    a credential-free remote identity) falls back to the git-common-dir
    digest instead of failing the delivery identity (fork decision 29).
    """

    remote = _git_command_output(path, "config", "--get", "remote.origin.url")
    if remote:
        try:
            return normalize_repository_identity(remote)
        except ValueError:
            pass
    return _local_repository_identity(path)


_TODO_WORKSPACE_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TODO_WORKSPACE_MAX_REPOS = 8


def _same_real_path(left: Path, right: Path) -> bool:
    return os.path.realpath(str(left)) == os.path.realpath(str(right))


def _todo_workspace_repo(
    repo_dir: Path, *, branch: str | None, declared_path: Any = None
) -> dict[str, Any] | None:
    """One repo of a per-Todo workspace, or ``None`` if it is not verifiable.

    The directory must be the top of a linked git worktree (not a main
    checkout) checked out on the Todo branch. When the Goal declares the
    repository, the worktree must belong to that repository. ``branch=None``
    (an acceptor's review checkout, fork G12) accepts a detached HEAD.
    """

    root = _git_worktree_root(repo_dir)
    if root is None or not _same_real_path(root, repo_dir):
        return None
    common = _git_common_dir(repo_dir)
    git_dir = _git_dir(repo_dir)
    if common is None or git_dir is None or common == git_dir:
        return None
    if branch is not None and _git_command_output(repo_dir, "symbolic-ref", "-q", "HEAD") != f"refs/heads/{branch}":
        return None
    head_sha = _git_command_output(repo_dir, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    repo_id = _git_repository_identity(repo_dir)
    if not head_sha or not repo_id:
        return None
    if declared_path is not None:
        declared = Path(str(declared_path)).expanduser()
        declared_common = _git_common_dir(declared) if declared.is_dir() else None
        if declared_common is None or declared_common != common:
            return None
    return {"head_sha": head_sha.lower(), "repo_id": repo_id}


def _is_review_checkout_root(path: Path, runtime_root: Path | str, goal_id: str, todo_id: str) -> bool:
    """Whether ``path`` is one attempt of this Todo's acceptor review checkout (fork G12).

    ``<runtime_root>/goals/<goal>/reviews/<todo>/<attempt>``: the dispatcher's
    throwaway detached checkout of the delivered commit, one repo per entry.
    """

    try:
        reviews = Path(runtime_root).expanduser() / "goals" / goal_id / "reviews" / todo_id
        return Path(path).is_dir() and _same_real_path(Path(os.path.realpath(str(path))).parent, reviews)
    except (OSError, TypeError, ValueError):
        return False


def verify_todo_workspace(
    path: Path,
    *,
    runtime_root: Path | str,
    goal_id: str,
    todo_id: str,
    goal: Mapping[str, Any] | None = None,
    repo_names: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Verify ``path`` is the registered S5 workspace root of exactly this Todo.

    Returns the ``todo_workspace`` identity (goal, Todo, branch and one
    ``{name, path, head_sha, repo_id}`` entry per repo, ``path`` relative to
    the workspace root) or ``None``. ``path`` must resolve to
    ``<runtime_root>/goals/<goal>/workspaces/<todo>``. Every listed repo (by
    default every git entry under the root) must be a linked worktree on the
    Todo branch ``loopx/<goal>/<todo>``; with ``goal`` it must also be a
    worktree of the repository the Goal declares under that name. Anything
    else fails closed. An acceptor's review checkout root of the same Todo
    (fork G12) is verified the same way, except that its repos are detached
    at the delivered commit instead of on the Todo branch.
    """

    from ...workspace.git_workspace import todo_branch, todo_workspace_root

    try:
        root = todo_workspace_root(runtime_root, goal_id, todo_id)
    except (OSError, TypeError, ValueError):
        return None
    review = _is_review_checkout_root(path, runtime_root, goal_id, todo_id)
    if not review and (not root.is_dir() or not _same_real_path(path, root)):
        return None
    if review:
        root = Path(os.path.realpath(str(path)))
    declared: dict[str, Any] | None = None
    if goal is not None:
        from ...workspace.repos import goal_repos

        try:
            declared = {str(repo["name"]): repo.get("path") for repo in goal_repos(goal)}
        except (KeyError, TypeError, ValueError):
            return None
    if repo_names is None:
        try:
            names = sorted(
                child.name for child in root.iterdir() if (child / ".git").exists()
            )
        except OSError:
            return None
    else:
        names = [str(name) for name in repo_names]
    if (
        not names
        or len(names) > _TODO_WORKSPACE_MAX_REPOS
        or len(set(names)) != len(names)
    ):
        return None
    branch = todo_branch(goal_id, todo_id)
    repos: list[dict[str, Any]] = []
    for name in names:
        if not _TODO_WORKSPACE_REPO_NAME.match(name):
            return None
        if declared is not None and name not in declared:
            return None
        verified = _todo_workspace_repo(
            root / name,
            branch=None if review else branch,
            declared_path=(declared or {}).get(name),
        )
        if verified is None:
            return None
        repos.append({"name": name, "path": name, **verified})
    return {"goal_id": goal_id, "todo_id": todo_id, "branch": branch, "repos": repos}


def _todo_workspace_revision_digest(todo_workspace: Mapping[str, Any]) -> str:
    material = "\0".join(
        f"{repo['name']}\0{repo['repo_id']}\0{repo['head_sha']}"
        for repo in todo_workspace["repos"]
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _capture_todo_workspace(
    path: Path,
    scope: Mapping[str, Any],
    *,
    peer_independent_worktree_required: bool,
    repository_source: str | None,
) -> dict[str, Any] | None:
    runtime_root = scope.get("runtime_root")
    goal_id = str(scope.get("goal_id") or "")
    todo_id = str(scope.get("todo_id") or "")
    if not runtime_root or not goal_id or not todo_id:
        return None
    goal = scope.get("goal")
    repo_names = scope.get("repo_names")
    todo_workspace = verify_todo_workspace(
        path,
        runtime_root=runtime_root,
        goal_id=goal_id,
        todo_id=todo_id,
        goal=goal if isinstance(goal, Mapping) else None,
        repo_names=list(repo_names) if isinstance(repo_names, (list, tuple)) else None,
    )
    if todo_workspace is None:
        return None
    return build_delivery_workspace_snapshot(
        workspace_identity=todo_workspace_identity_ref(goal_id, todo_id),
        identity_kind="todo_workspace",
        repository_source=repository_source or "todo_workspace_root",
        workspace_kind="todo_workspace_root",
        peer_independent_worktree_required=peer_independent_worktree_required,
        workspace_revision_digest=_todo_workspace_revision_digest(todo_workspace),
        todo_workspace=todo_workspace,
    )


def capture_delivery_workspace(
    current_path: Path | None = None,
    *,
    peer_independent_worktree_required: bool = False,
    local_goal_id: str | None = None,
    local_project_root: Path | None = None,
    repository_source: str | None = None,
    todo_workspace_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Capture a compact, credential-free delivery workspace identity.

    Git deliveries bind to their repository identity (the canonical origin,
    or a local git-common-dir digest without one) and, when HEAD exists, an
    opaque digest of its content-addressed revision. A single-agent non-Git
    goal may instead bind to its stable LoopX goal identity. These snapshots
    exclude local paths, branch names and raw commits.

    With ``todo_workspace_scope`` (``runtime_root``, ``goal_id``, ``todo_id``
    and optionally ``goal`` and ``repo_names``), a path that is the registered
    fork S5 workspace root of exactly that Todo binds to a ``todo_workspace``
    identity: goal, Todo, the Todo branch and per repo its root-relative
    path, HEAD and repo id (fork decision 29).
    """

    path = current_path or Path.cwd()
    current_root = _git_worktree_root(path)
    if current_root is None and todo_workspace_scope:
        todo_snapshot = _capture_todo_workspace(
            path,
            todo_workspace_scope,
            peer_independent_worktree_required=peer_independent_worktree_required,
            repository_source=repository_source,
        )
        if todo_snapshot is not None:
            return todo_snapshot
    if current_root is None:
        if peer_independent_worktree_required or not local_goal_id:
            return None
        if local_project_root is not None and not _is_same_or_child_path(
            path, local_project_root
        ):
            return None
        try:
            workspace_identity = resolve_project_identity(
                path,
                loopx_project_id=local_goal_id,
            )
        except ValueError:
            return None
        if not workspace_identity.startswith("loopx:"):
            return None
        return build_delivery_workspace_snapshot(
            workspace_identity=workspace_identity,
            identity_kind="local_goal",
            repository_source=repository_source or "goal_id_fallback",
            workspace_kind="local_goal_workspace",
            peer_independent_worktree_required=False,
        )
    current_common = _git_common_dir(path)
    current_git_dir = _git_dir(path)
    task_repository = _git_repository_identity(path)
    workspace_revision = _git_command_output(path, "rev-parse", "HEAD")
    if (
        not task_repository
        or current_common is None
        or current_git_dir is None
    ):
        return None
    workspace_revision_digest = (
        sha256(f"{task_repository}\0{workspace_revision}".encode()).hexdigest()
        if workspace_revision is not None
        else None
    )
    workspace_kind = (
        "independent_git_worktree"
        if current_git_dir != current_common
        else "canonical_checkout"
    )
    return build_delivery_workspace_snapshot(
        workspace_identity=task_repository,
        identity_kind="git_repository",
        repository_source=repository_source or (
            "current_git_origin"
            if task_repository.startswith("git:")
            else "current_git_common_dir"
        ),
        workspace_kind=workspace_kind,
        peer_independent_worktree_required=peer_independent_worktree_required,
        workspace_revision_digest=workspace_revision_digest,
    )


def delivery_workspace_identity(value: Any) -> str | None:
    snapshot = normalize_delivery_workspace_snapshot(value)
    if snapshot is None:
        return None
    return str(snapshot["workspace_identity"])


def delivery_workspace_repository(value: Any) -> str | None:
    snapshot = normalize_delivery_workspace_snapshot(value)
    if snapshot is None or snapshot.get("identity_kind") != "git_repository":
        return None
    repository = str(snapshot.get("task_repository") or "")
    if _LOCAL_REPOSITORY_ID.match(repository):
        return repository
    try:
        return normalize_todo_task_repository(repository)
    except (TypeError, ValueError):
        return None


_LOCAL_REPOSITORY_ID = re.compile(r"^local:[0-9a-f]{64}$")


def _todo_workspace_delivery_guard(
    snapshot: Mapping[str, Any],
    delivery_run: Mapping[str, Any],
    *,
    agent_id: str | None,
    current_path: Path | None,
    runtime_root: Path | str | None,
) -> dict[str, Any] | None:
    """Spend from the recorded per-Todo workspace root, with the same repos."""

    recorded = snapshot.get("todo_workspace")
    recorded = recorded if isinstance(recorded, Mapping) else {}
    recorded_repos = {
        str(repo.get("name")): repo.get("repo_id")
        for repo in recorded.get("repos") or []
        if isinstance(repo, Mapping)
    }
    current = (
        capture_delivery_workspace(
            current_path,
            todo_workspace_scope={
                "runtime_root": runtime_root,
                "goal_id": recorded.get("goal_id"),
                "todo_id": recorded.get("todo_id"),
                "repo_names": sorted(recorded_repos),
            },
        )
        if runtime_root is not None and recorded_repos
        else None
    )
    current_todo_workspace = (current or {}).get("todo_workspace")
    current_repos = (
        {
            str(repo.get("name")): repo.get("repo_id")
            for repo in current_todo_workspace.get("repos") or []
            if isinstance(repo, Mapping)
        }
        if isinstance(current_todo_workspace, Mapping)
        else {}
    )
    if (
        current is not None
        and current.get("workspace_identity") == snapshot.get("workspace_identity")
        and current_repos == recorded_repos
    ):
        return None
    return {
        "schema_version": AGENT_WORKSPACE_GUARD_SCHEMA_VERSION,
        "source": "quota.spend_slot.delivery_workspace",
        "action": "return_to_delivery_worktree",
        "current_workspace": (
            "foreign_todo_workspace" if current is not None else "not_todo_workspace_root"
        ),
        "required_workspace": "accountable_delivery_todo_workspace_root",
        "blocks_delivery": True,
        "agent_id": normalize_todo_claimed_by(agent_id),
        "repository_source": "delivery_run.delivery_workspace.todo_workspace",
        "workspace_identity": snapshot.get("workspace_identity"),
        "delivery_run_generated_at": delivery_run.get("generated_at"),
        "delivery_run_classification": delivery_run.get("classification"),
        "reason": (
            "quota spend workspace does not match the latest unspent accountable "
            "delivery workspace"
        ),
        "required_action": (
            "run quota spend-slot from the per-Todo workspace root that produced "
            "the latest unspent accountable delivery"
        ),
    }


def build_delivery_workspace_guard(
    delivery_run: dict[str, Any],
    *,
    agent_id: str | None = None,
    current_path: Path | None = None,
    runtime_root: Path | str | None = None,
) -> dict[str, Any] | None:
    """Validate quota accounting against its accountable delivery workspace.

    Legacy delivery runs without a snapshot return ``None`` so the existing
    selected-todo workspace guard remains the fail-closed fallback. A
    ``todo_workspace`` delivery must be spent from the same per-Todo
    workspace root (under ``runtime_root``) with the same repositories.
    """

    snapshot = (
        delivery_run.get("delivery_workspace")
        if isinstance(delivery_run.get("delivery_workspace"), dict)
        else {}
    )
    normalized_snapshot = normalize_delivery_workspace_snapshot(snapshot)
    if normalized_snapshot is None:
        return None
    if normalized_snapshot["identity_kind"] == "local_goal":
        return None
    if normalized_snapshot["identity_kind"] == "todo_workspace":
        return _todo_workspace_delivery_guard(
            normalized_snapshot,
            delivery_run,
            agent_id=agent_id,
            current_path=current_path,
            runtime_root=runtime_root,
        )
    task_repository = str(normalized_snapshot["task_repository"])

    current = capture_delivery_workspace(current_path)
    current_repository = delivery_workspace_repository(current) or ""
    current_workspace = str((current or {}).get("workspace_kind") or "")
    recorded_workspace = str(normalized_snapshot.get("workspace_kind") or "")
    peer_independent_worktree_required = bool(
        normalized_snapshot.get("peer_independent_worktree_required")
    )
    if not current:
        current_workspace = "not_git_worktree"
    elif current_repository != task_repository:
        current_workspace = "foreign_git_worktree"
    elif (
        recorded_workspace == "independent_git_worktree"
        and current_workspace != "independent_git_worktree"
    ):
        current_workspace = "canonical_checkout"
    elif (
        peer_independent_worktree_required
        and recorded_workspace != "independent_git_worktree"
    ):
        current_workspace = "delivery_not_recorded_from_independent_worktree"
    else:
        return None

    return {
        "schema_version": AGENT_WORKSPACE_GUARD_SCHEMA_VERSION,
        "source": "quota.spend_slot.delivery_workspace",
        "action": "return_to_delivery_worktree",
        "current_workspace": current_workspace,
        "required_workspace": (
            "accountable_delivery_independent_git_worktree"
            if recorded_workspace == "independent_git_worktree"
            or peer_independent_worktree_required
            else "accountable_delivery_git_checkout"
        ),
        "blocks_delivery": True,
        "agent_id": normalize_todo_claimed_by(agent_id),
        "repository_source": "delivery_run.delivery_workspace.task_repository",
        "task_repository": task_repository,
        "delivery_run_generated_at": delivery_run.get("generated_at"),
        "delivery_run_classification": delivery_run.get("classification"),
        "reason": (
            "quota spend workspace does not match the latest unspent accountable "
            "delivery workspace"
        ),
        "required_action": (
            "run quota spend-slot from the workspace that produced the latest "
            "unspent accountable delivery"
        ),
    }


def _peer_candidate_items(agent_todo_summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(agent_todo_summary, dict):
        return []
    for key in (
        "active_next_action_executable_items",
        "executable_backlog_items",
        "first_executable_items",
    ):
        items = agent_todo_summary.get(key)
        if isinstance(items, list) and items:
            return [item for item in items if isinstance(item, dict)]
    return []


def _peer_work_requires_isolated_workspace(
    workspace_guard_policy: dict[str, Any],
    agent_todo_summary: dict[str, Any] | None,
    *,
    selected_todo: dict[str, Any] | None = None,
) -> bool:
    explicit = workspace_guard_policy.get("peer_independent_worktree_required")
    if explicit is not None:
        return explicit is True
    candidate = (
        selected_todo
        if isinstance(selected_todo, dict) and selected_todo
        else next(iter(_peer_candidate_items(agent_todo_summary)), None)
    )
    if not isinstance(candidate, dict):
        return False
    if candidate.get("required_write_scopes"):
        return True
    if str(candidate.get("task_class") or "").strip().lower() == "continuous_monitor":
        return False
    action_kind = str(candidate.get("action_kind") or "").strip().lower()
    return action_kind in PEER_WRITE_ACTION_KINDS or action_kind.startswith(
        tuple(f"{prefix}_" for prefix in PEER_WRITE_ACTION_KINDS)
    )


def _in_registered_todo_workspace(
    goal: Mapping[str, Any],
    todo: Mapping[str, Any],
    *,
    current_path: Path,
    runtime_root: Path | str | None,
) -> bool:
    """Whether ``current_path`` is this S5 Todo's verified workspace.

    That is its registered workspace root, or for a one-repo Todo that repo's
    worktree inside it. Every repo the Todo names must be a worktree of the
    Goal-declared repository on the Todo branch.
    """

    repo_names = normalize_todo_task_repositories(todo.get("task_repositories"))
    todo_id = str(todo.get("todo_id") or "").strip()
    goal_id = str(goal.get("id") or "").strip()
    if not repo_names or not todo_id or not goal_id or runtime_root is None:
        return False
    from ...workspace.git_workspace import todo_workspace_root

    try:
        root = todo_workspace_root(runtime_root, goal_id, todo_id)
    except (OSError, TypeError, ValueError):
        return False
    review_root = current_path if len(repo_names) > 1 else current_path.parent
    if _is_review_checkout_root(review_root, runtime_root, goal_id, todo_id):
        # Fork G12: the acceptor reviews in a detached checkout of the delivery.
        root = review_root
    elif not _same_real_path(current_path, root) and not (
        len(repo_names) == 1 and _same_real_path(current_path, root / repo_names[0])
    ):
        return False
    return (
        verify_todo_workspace(
            root,
            runtime_root=runtime_root,
            goal_id=goal_id,
            todo_id=todo_id,
            goal=goal,
            repo_names=repo_names,
        )
        is not None
    )


def build_agent_workspace_guard(
    goal: dict[str, Any],
    agent_identity: dict[str, Any] | None,
    *,
    agent_todo_summary: dict[str, Any] | None = None,
    selected_todo: dict[str, Any] | None = None,
    current_path: Path | None = None,
    runtime_root: Path | str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(agent_identity, dict):
        return None
    workspace_guard_policy = (
        goal.get("workspace_guard_policy")
        if isinstance(goal.get("workspace_guard_policy"), dict)
        else {}
    )
    if len(agent_identity.get("registered_agents") or []) <= 1:
        return None
    if not _peer_work_requires_isolated_workspace(
        workspace_guard_policy,
        agent_todo_summary,
        selected_todo=selected_todo,
    ):
        return None
    current_path = current_path or Path.cwd()
    candidate = (
        selected_todo
        if isinstance(selected_todo, dict) and selected_todo
        else next(iter(_peer_candidate_items(agent_todo_summary)), {})
    )
    # The rule that decides the refresh-state delivery workspace decides here too.
    requirement = peer_delivery_workspace(
        goal, agent_id=agent_identity.get("agent_id"), multi_agent=True, todo=candidate or None,
    )
    if requirement is PeerDeliveryWorkspace.ANY:
        return None
    goal_project = goal_project_directory(goal)
    if (
        requirement is PeerDeliveryWorkspace.GOAL_PROJECT_DIRECTORY
        and goal_project is not None
        and _is_same_or_child_path(current_path, goal_project)
    ):
        return None
    task_repository = normalize_todo_task_repository(candidate.get("task_repository"))
    if not task_repository and _in_registered_todo_workspace(
        goal, candidate, current_path=current_path, runtime_root=runtime_root
    ):
        # Fork S5: the per-Todo workspace (one worktree per repo on the Todo
        # branch) is this Todo's independent workspace.
        return None
    current_workspace = ""
    repository_source = "goal.repo"
    if task_repository:
        repository_source = "selected_todo.task_repository"
        current_root = _git_worktree_root(current_path)
        current_common = _git_common_dir(current_path) if current_root else None
        current_git_dir = _git_dir(current_path) if current_root else None
        current_repository = (
            _git_repository_identity(current_path) if current_root else None
        )
        if current_root is None:
            current_workspace = "not_git_worktree"
        elif current_repository != task_repository:
            current_workspace = "foreign_git_worktree"
        elif (
            current_common is None
            or current_git_dir is None
            or current_git_dir == current_common
        ):
            current_workspace = "canonical_checkout"
    else:
        repo_value = goal.get("repo") or goal.get("project") or goal.get("root")
        if not repo_value:
            return None
        repo_path = Path(str(repo_value)).expanduser()
        if not repo_path.is_absolute():
            return None
        if _is_same_or_child_path(current_path, repo_path):
            current_workspace = "canonical_checkout"
        else:
            canonical_root = _git_worktree_root(repo_path) or repo_path
            current_root = _git_worktree_root(current_path)
            canonical_common = _git_common_dir(canonical_root)
            current_common = _git_common_dir(current_path) if current_root else None
            if current_root is None:
                current_workspace = "not_git_worktree"
            elif (
                canonical_common is None
                or current_common is None
                or current_common != canonical_common
            ):
                current_workspace = "foreign_git_worktree"
            elif current_root == canonical_root:
                current_workspace = "canonical_checkout"
    if not current_workspace:
        return None
    payload = {
        "schema_version": AGENT_WORKSPACE_GUARD_SCHEMA_VERSION,
        "source": "quota.should-run",
        "action": "move_to_independent_worktree",
        "current_workspace": current_workspace,
        "required_workspace": "independent_git_worktree",
        "blocks_delivery": True,
        "agent_id": agent_identity.get("agent_id"),
        "repository_source": repository_source,
        "reason": (
            "peer delivery with repository writes is not running from an independent "
            "worktree; normal delivery must move before repository edits"
        ),
        "required_action": (
            "create or switch to an independent git worktree/branch for this peer lane, "
            "then rerun quota should-run with the same --agent-id before editing files"
        ),
    }
    if task_repository:
        payload["task_repository"] = task_repository
    return payload
