import type { JsonObject } from "../effect_program.ts";
import { EffectRuntimeRequestError } from "../effect_runtime_errors.ts";
import {
  optionalNonEmptyString,
  requireBoolean,
  requireJsonObject,
  requireNonEmptyString,
  requireStringLiteral,
} from "../runtime_decode.ts";
import {
  DELIVERY_WORKSPACE_SNAPSHOT_LEGACY_SNAPSHOT_SCHEMA,
  DELIVERY_WORKSPACE_SNAPSHOT_REQUEST_SCHEMA,
  DELIVERY_WORKSPACE_SNAPSHOT_RESULT_SCHEMA,
  DELIVERY_WORKSPACE_SNAPSHOT_SNAPSHOT_SCHEMA,
} from "../coordination/coordination_state_contract.generated.ts";

export const DELIVERY_WORKSPACE_SCHEMA_VERSION =
  DELIVERY_WORKSPACE_SNAPSHOT_SNAPSHOT_SCHEMA;
export const LEGACY_DELIVERY_WORKSPACE_SCHEMA_VERSION =
  DELIVERY_WORKSPACE_SNAPSHOT_LEGACY_SNAPSHOT_SCHEMA;
export const DELIVERY_WORKSPACE_REQUEST_SCHEMA =
  DELIVERY_WORKSPACE_SNAPSHOT_REQUEST_SCHEMA;
export const DELIVERY_WORKSPACE_RESULT_SCHEMA =
  DELIVERY_WORKSPACE_SNAPSHOT_RESULT_SCHEMA;

export const DELIVERY_WORKSPACE_IDENTITY_KINDS = [
  "git_repository",
  "local_goal",
  "todo_workspace",
] as const;
export type DeliveryWorkspaceIdentityKind =
  (typeof DELIVERY_WORKSPACE_IDENTITY_KINDS)[number];

export const DELIVERY_WORKSPACE_KINDS = [
  "canonical_checkout",
  "independent_git_worktree",
  "local_goal_workspace",
  "todo_workspace_root",
] as const;
export type DeliveryWorkspaceKind =
  (typeof DELIVERY_WORKSPACE_KINDS)[number];

/** One repository of a fork S5 per-Todo workspace (design decision 29). */
export interface TodoWorkspaceRepo extends JsonObject {
  name: string;
  /** Repository directory relative to the Todo workspace root. */
  path: string;
  head_sha: string;
  /** Canonical origin identity, else ``local:`` + sha256 of the git common dir. */
  repo_id: string;
}

/**
 * The per-Todo workspace a multi-repo delivery ran in: goal, Todo, the shared
 * Todo branch and one entry per repository. Only a ``todo_workspace``
 * snapshot carries it; single-repo snapshots keep their original shape.
 */
export interface TodoWorkspaceIdentity extends JsonObject {
  goal_id: string;
  todo_id: string;
  branch: string;
  repos: TodoWorkspaceRepo[];
}

export interface DeliveryWorkspaceSnapshot extends JsonObject {
  schema_version: typeof DELIVERY_WORKSPACE_SCHEMA_VERSION;
  workspace_identity: string;
  identity_kind: DeliveryWorkspaceIdentityKind;
  task_repository: string | null;
  workspace_revision_digest?: string;
  repository_source: string;
  workspace_kind: DeliveryWorkspaceKind;
  peer_independent_worktree_required: boolean;
  todo_workspace?: TodoWorkspaceIdentity;
}

type DeliveryWorkspaceOperation = "build" | "normalize";

const GIT_IDENTITY_PATTERN =
  /^git:[a-z0-9.-]+(?::[0-9]{1,5})?\/[A-Za-z0-9._~+/-]+$/i;
const LOCAL_GOAL_IDENTITY_PATTERN =
  /^loopx:[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const GIT_REVISION_DIGEST_PATTERN = /^[0-9a-f]{64}$/i;
const LOCAL_REPOSITORY_ID_PATTERN = /^local:[0-9a-f]{64}$/;
const TODO_WORKSPACE_IDENTITY_PREFIX = "todo-workspace:";
const WORKSPACE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const REPO_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const HEAD_SHA_PATTERN = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;
const TODO_WORKSPACE_MAX_REPOS = 8;

function operation(value: unknown): DeliveryWorkspaceOperation {
  return requireStringLiteral(
    value,
    ["build", "normalize"] as const,
    "delivery workspace operation",
    "delivery workspace operation is unsupported",
  );
}

function requestObject(value: unknown): JsonObject {
  const request = requireJsonObject(value, "delivery workspace request");
  if (request.schema_version !== DELIVERY_WORKSPACE_REQUEST_SCHEMA) {
    throw new EffectRuntimeRequestError("delivery workspace request schema mismatch");
  }
  return request;
}

function canonicalGitIdentity(value: unknown, label: string): string | null {
  const identity = optionalNonEmptyString(value, label);
  if (!identity || !GIT_IDENTITY_PATTERN.test(identity)) return null;
  const suffix = identity.slice("git:".length);
  const slash = suffix.indexOf("/");
  if (slash <= 0) return null;
  const host = suffix.slice(0, slash).toLowerCase();
  let path = suffix.slice(slash + 1).replace(/\/{2,}/g, "/");
  if (path.endsWith(".git")) path = path.slice(0, -4);
  if (
    !path || path.split("/").some((segment) => segment === "." || segment === "..")
  ) return null;
  return `git:${host}/${path}`;
}

/**
 * A delivery repository identity: the canonical origin identity, or for a
 * repository without ``origin`` a stable local identity (``local:`` + sha256
 * of the realpath of its git common dir).
 */
function repositoryIdentity(value: unknown, label: string): string | null {
  const identity = optionalNonEmptyString(value, label);
  if (identity && LOCAL_REPOSITORY_ID_PATTERN.test(identity)) return identity;
  return canonicalGitIdentity(value, label);
}

function validWorkspaceId(value: unknown): value is string {
  return typeof value === "string" && WORKSPACE_ID_PATTERN.test(value) &&
    !value.includes("..") && !value.endsWith(".lock") && !value.endsWith(".");
}

export function todoWorkspaceIdentityRef(goalId: string, todoId: string): string {
  return `${TODO_WORKSPACE_IDENTITY_PREFIX}${goalId}/${todoId}`;
}

function todoWorkspaceRepo(value: unknown): TodoWorkspaceRepo | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const candidate = value as JsonObject;
  const name = candidate.name;
  const path = candidate.path;
  const headSha = candidate.head_sha;
  if (
    typeof name !== "string" || !REPO_NAME_PATTERN.test(name) ||
    typeof path !== "string" || typeof headSha !== "string" ||
    !HEAD_SHA_PATTERN.test(headSha.toLowerCase())
  ) return null;
  // A repository directory is named relative to the Todo workspace root; the
  // snapshot never records an absolute local path.
  const segments = path.split("/");
  if (
    !path || path.startsWith("/") || path.includes("\\") ||
    segments.some((segment) => !segment || segment === "." || segment === "..")
  ) return null;
  const repoId = typeof candidate.repo_id === "string"
    ? repositoryIdentity(candidate.repo_id, "todo_workspace.repos.repo_id")
    : null;
  if (!repoId) return null;
  return { name, path, head_sha: headSha.toLowerCase(), repo_id: repoId };
}

function todoWorkspaceIdentity(value: unknown): TodoWorkspaceIdentity | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const candidate = value as JsonObject;
  const goalId = candidate.goal_id;
  const todoId = candidate.todo_id;
  if (!validWorkspaceId(goalId) || !validWorkspaceId(todoId)) return null;
  if (candidate.branch !== `loopx/${goalId}/${todoId}`) return null;
  if (
    !Array.isArray(candidate.repos) || candidate.repos.length === 0 ||
    candidate.repos.length > TODO_WORKSPACE_MAX_REPOS
  ) return null;
  const repos: TodoWorkspaceRepo[] = [];
  const names = new Set<string>();
  for (const item of candidate.repos) {
    const repo = todoWorkspaceRepo(item);
    if (!repo || names.has(repo.name)) return null;
    names.add(repo.name);
    repos.push(repo);
  }
  return { goal_id: goalId, todo_id: todoId, branch: candidate.branch, repos };
}

function localGoalIdentity(value: unknown, label: string): string | null {
  const identity = optionalNonEmptyString(value, label);
  return identity && LOCAL_GOAL_IDENTITY_PATTERN.test(identity) ? identity : null;
}

function snapshot(
  workspaceIdentity: string,
  identityKind: DeliveryWorkspaceIdentityKind,
  workspaceRevisionDigest: string | null,
  repositorySource: string,
  workspaceKind: DeliveryWorkspaceKind,
  peerIndependentWorktreeRequired: boolean,
  rawTodoWorkspace: unknown = undefined,
): DeliveryWorkspaceSnapshot | null {
  if (identityKind === "todo_workspace") {
    const todoWorkspace = todoWorkspaceIdentity(rawTodoWorkspace);
    if (
      !todoWorkspace ||
      workspaceIdentity !==
        todoWorkspaceIdentityRef(todoWorkspace.goal_id, todoWorkspace.todo_id) ||
      workspaceKind !== "todo_workspace_root" ||
      (workspaceRevisionDigest !== null &&
        !GIT_REVISION_DIGEST_PATTERN.test(workspaceRevisionDigest))
    ) return null;
    return {
      schema_version: DELIVERY_WORKSPACE_SCHEMA_VERSION,
      workspace_identity: workspaceIdentity,
      identity_kind: identityKind,
      task_repository: null,
      ...(workspaceRevisionDigest === null
        ? {}
        : { workspace_revision_digest: workspaceRevisionDigest.toLowerCase() }),
      repository_source: repositorySource,
      workspace_kind: workspaceKind,
      peer_independent_worktree_required: peerIndependentWorktreeRequired,
      todo_workspace: todoWorkspace,
    };
  }
  // Only a todo_workspace snapshot carries the per-repo list.
  if (rawTodoWorkspace !== undefined && rawTodoWorkspace !== null) return null;
  if (identityKind === "git_repository") {
    const taskRepository = repositoryIdentity(
      workspaceIdentity,
      "workspace_identity",
    );
    if (
      !taskRepository ||
      (workspaceRevisionDigest !== null &&
        !GIT_REVISION_DIGEST_PATTERN.test(workspaceRevisionDigest)) ||
      (workspaceKind !== "canonical_checkout" &&
        workspaceKind !== "independent_git_worktree")
    ) return null;
    return {
      schema_version: DELIVERY_WORKSPACE_SCHEMA_VERSION,
      workspace_identity: taskRepository,
      identity_kind: identityKind,
      task_repository: taskRepository,
      ...(workspaceRevisionDigest === null
        ? {}
        : { workspace_revision_digest: workspaceRevisionDigest.toLowerCase() }),
      repository_source: repositorySource,
      workspace_kind: workspaceKind,
      peer_independent_worktree_required: peerIndependentWorktreeRequired,
    };
  }

  const localIdentity = localGoalIdentity(workspaceIdentity, "workspace_identity");
  if (
    !localIdentity || workspaceRevisionDigest !== null ||
    workspaceKind !== "local_goal_workspace" ||
    peerIndependentWorktreeRequired
  ) return null;
  return {
    schema_version: DELIVERY_WORKSPACE_SCHEMA_VERSION,
    workspace_identity: localIdentity,
    identity_kind: identityKind,
    task_repository: null,
    repository_source: repositorySource,
    workspace_kind: workspaceKind,
    peer_independent_worktree_required: false,
  };
}

export function buildDeliveryWorkspaceSnapshot(
  value: unknown,
): DeliveryWorkspaceSnapshot | null {
  const candidate = requireJsonObject(value, "delivery workspace observation");
  const identityKind = requireStringLiteral(
    candidate.identity_kind,
    DELIVERY_WORKSPACE_IDENTITY_KINDS,
    "identity_kind",
  );
  const workspaceKind = requireStringLiteral(
    candidate.workspace_kind,
    DELIVERY_WORKSPACE_KINDS,
    "workspace_kind",
  );
  return snapshot(
    requireNonEmptyString(candidate.workspace_identity, "workspace_identity"),
    identityKind,
    optionalNonEmptyString(
      candidate.workspace_revision_digest,
      "workspace_revision_digest",
    ),
    requireNonEmptyString(candidate.repository_source, "repository_source"),
    workspaceKind,
    requireBoolean(
      candidate.peer_independent_worktree_required,
      "peer_independent_worktree_required",
    ),
    candidate.todo_workspace,
  );
}

export function normalizeDeliveryWorkspaceSnapshot(
  value: unknown,
): DeliveryWorkspaceSnapshot | null {
  if (value === null || value === undefined) return null;
  const candidate = requireJsonObject(value, "delivery workspace snapshot");
  const schemaVersion = optionalNonEmptyString(
    candidate.schema_version,
    "schema_version",
  );
  if (schemaVersion === LEGACY_DELIVERY_WORKSPACE_SCHEMA_VERSION) {
    const taskRepository = canonicalGitIdentity(
      candidate.task_repository,
      "task_repository",
    );
    if (!taskRepository) return null;
    const workspaceKind = requireStringLiteral(
      candidate.workspace_kind,
      ["canonical_checkout", "independent_git_worktree"] as const,
      "workspace_kind",
    );
    return snapshot(
      taskRepository,
      "git_repository",
      null,
      requireNonEmptyString(candidate.repository_source, "repository_source"),
      workspaceKind,
      requireBoolean(
        candidate.peer_independent_worktree_required,
        "peer_independent_worktree_required",
      ),
    );
  }
  if (schemaVersion !== DELIVERY_WORKSPACE_SCHEMA_VERSION) return null;

  const identityKind = requireStringLiteral(
    candidate.identity_kind,
    DELIVERY_WORKSPACE_IDENTITY_KINDS,
    "identity_kind",
  );
  const workspaceKind = requireStringLiteral(
    candidate.workspace_kind,
    DELIVERY_WORKSPACE_KINDS,
    "workspace_kind",
  );
  const normalized = snapshot(
    requireNonEmptyString(candidate.workspace_identity, "workspace_identity"),
    identityKind,
    optionalNonEmptyString(
      candidate.workspace_revision_digest,
      "workspace_revision_digest",
    ),
    requireNonEmptyString(candidate.repository_source, "repository_source"),
    workspaceKind,
    requireBoolean(
      candidate.peer_independent_worktree_required,
      "peer_independent_worktree_required",
    ),
    candidate.todo_workspace,
  );
  if (!normalized) return null;
  const declaredRepository = optionalNonEmptyString(
    candidate.task_repository,
    "task_repository",
  );
  if (
    declaredRepository !== null &&
    repositoryIdentity(declaredRepository, "task_repository") !==
      normalized.task_repository
  ) return null;
  return normalized;
}

export function evaluateDeliveryWorkspace(value: unknown): JsonObject {
  const request = requestObject(value);
  const selectedOperation = operation(request.operation);
  return {
    schema_version: DELIVERY_WORKSPACE_RESULT_SCHEMA,
    workspace: selectedOperation === "build"
      ? buildDeliveryWorkspaceSnapshot(request.observation)
      : normalizeDeliveryWorkspaceSnapshot(request.workspace),
  };
}
