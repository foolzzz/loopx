from __future__ import annotations

import os
from pathlib import Path


RUNTIME_ROOT_ENV = "LOOPX_RUNTIME_ROOT"
LOOPX_STATE_DIRNAME = ".loopx"
CODEX_HOME_DIRNAME = ".codex"
DEFAULT_PROJECT_REGISTRY = Path(LOOPX_STATE_DIRNAME) / "registry.json"
PROJECT_GOAL_STATE_ROOT = Path(LOOPX_STATE_DIRNAME) / "goals"
COLLOCATED_PROJECT_GOAL_STATE_ROOT = Path(LOOPX_STATE_DIRNAME) / "project-goals"
ACTIVE_GOAL_STATE_FILENAME = "ACTIVE_GOAL_STATE.md"
GLOBAL_REGISTRY_FILENAME = "registry.global.json"
# default_runtime_root() spelled for a POSIX shell, for commands LoopX renders
# for another shell to run (agent prompts, SSH commands). The shell that runs
# the command expands it: an unset or empty LOOPX_RUNTIME_ROOT means ~/.loopx,
# and a relative value names a path under that shell's working directory,
# which is what default_runtime_root() yields in a process started there.
# Unlike the Python resolver the shell does not trim whitespace. Rendered
# commands only pass it where a plain path is read (--registry, mkdir), never
# as --runtime-root, whose relative values resolve against the registry's
# project instead.
SHELL_DEFAULT_RUNTIME_ROOT = f"${{{RUNTIME_ROOT_ENV}:-$HOME/{LOOPX_STATE_DIRNAME}}}"
SHELL_DEFAULT_GLOBAL_REGISTRY = f"{SHELL_DEFAULT_RUNTIME_ROOT}/{GLOBAL_REGISTRY_FILENAME}"


def project_state_path(project: Path, *parts: str) -> Path:
    """Build a lexical project-owned path, independent of the runtime root.

    Callers retain their own expansion, resolution and containment checks.
    Goal state must use :func:`project_goal_state_dir` for overlap protection.
    """

    return Path(project) / LOOPX_STATE_DIRNAME / Path(*parts)


def project_registry_path(project: Path) -> Path:
    """Return the conventional project registry without environment overrides."""

    return Path(project) / DEFAULT_PROJECT_REGISTRY


def home_codex_root(home: Path | None = None) -> Path:
    """Return Codex's conventional store under a home, ignoring CODEX_HOME."""

    return (Path.home() if home is None else Path(home)) / CODEX_HOME_DIRNAME


def codex_home_path(value: str | Path | None = None) -> Path:
    """Resolve an explicit Codex home, then CODEX_HOME, then its home default.

    Preserve relative paths and whitespace; resolving or pinning the store
    remains the caller's responsibility, separate from LoopX runtime state.
    """

    return Path(value or os.environ.get("CODEX_HOME") or home_codex_root()).expanduser()


def home_runtime_root(home: Path | None = None) -> Path:
    """Return the runtime root LoopX uses under ``home`` when none is configured."""

    return (Path.home() if home is None else Path(home)) / LOOPX_STATE_DIRNAME


def configured_runtime_root() -> Path | None:
    """Return the runtime root named by ``LOOPX_RUNTIME_ROOT``, made absolute.

    An unset or blank value configures nothing. A relative value is resolved
    against the current directory at the moment it is read, lexically, so the
    same value names the same directory for every consumer in the process.
    """

    value = os.environ.get(RUNTIME_ROOT_ENV, "").strip()
    if not value:
        return None
    return Path(os.path.abspath(os.path.expanduser(value)))


def default_runtime_root() -> Path:
    """Return the runtime root used when no explicit root is given.

    ``LOOPX_RUNTIME_ROOT`` when it is set, otherwise ``~/.loopx``. It is read at
    call time, never cached at import, so HOME and the environment in effect
    when the root is needed decide it. An explicit ``--runtime-root`` and a
    registry's ``common_runtime_root`` take precedence over this default; see
    :func:`resolve_runtime_root`.
    """

    return configured_runtime_root() or home_runtime_root()


def project_goal_state_dir(
    project: Path,
    goal_id: str,
    *,
    runtime_root: Path | None = None,
) -> Path:
    """Return the project-owned goal state directory.

    A project rooted at HOME would otherwise put project state in the same
    ``~/.loopx/goals`` tree that the runtime owns. Keep the conventional path
    unless those physical roots coincide, then use a distinct project-owned
    namespace.
    """

    project = Path(project)
    state_root = project / PROJECT_GOAL_STATE_ROOT
    resolved_runtime_root = (
        default_runtime_root() if runtime_root is None else Path(runtime_root)
    )
    try:
        roots_overlap = (
            state_root.resolve() == (resolved_runtime_root / "goals").resolve()
        )
    except OSError:
        roots_overlap = (
            state_root.absolute() == (resolved_runtime_root / "goals").absolute()
        )
    if roots_overlap:
        state_root = project / COLLOCATED_PROJECT_GOAL_STATE_ROOT

    return state_root / goal_id


def project_goal_state_file(
    project: Path,
    goal_id: str,
    *,
    runtime_root: Path | None = None,
) -> Path:
    """Return a project's default active goal state file for ``goal_id``."""

    return (
        project_goal_state_dir(project, goal_id, runtime_root=runtime_root)
        / ACTIVE_GOAL_STATE_FILENAME
    )


def default_public_scan_root() -> str:
    """Return the bounded LoopX package root used by public scans."""

    return str(Path(__file__).resolve().parent)


def default_registry_path() -> Path:
    value = os.environ.get("LOOPX_REGISTRY")
    if value:
        return Path(value).expanduser()
    return DEFAULT_PROJECT_REGISTRY


def global_registry_path(runtime_root: Path | None = None) -> Path:
    return (default_runtime_root() if runtime_root is None else runtime_root) / GLOBAL_REGISTRY_FILENAME


def registry_project_root(registry_path: Path) -> Path:
    """Return the project root that owns a registry path.

    Project registries conventionally live at ``<project>/.loopx/registry.json``.
    Standalone fixtures and global registries live directly under their owning
    root.  Keeping this rule here prevents relative runtime paths from silently
    depending on the caller's current working directory.
    """

    expanded = registry_path.expanduser().resolve()
    parent = expanded.parent
    return parent.parent if parent.name == LOOPX_STATE_DIRNAME else parent


def resolve_runtime_root(
    registry: dict[str, object],
    override: str | None = None,
    *,
    registry_path: Path | None = None,
) -> Path:
    """Return the runtime root for ``registry``.

    Precedence: an explicit ``override`` (``--runtime-root``), then the
    registry's ``common_runtime_root``, then :func:`default_runtime_root`.
    """

    value = override
    if not value:
        value = registry.get("common_runtime_root") if isinstance(registry, dict) else None
    if not value:
        return default_runtime_root()

    runtime_root = Path(str(value)).expanduser()
    if runtime_root.is_absolute() or registry_path is None:
        return runtime_root
    return registry_project_root(registry_path) / runtime_root


def rel_or_abs(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
