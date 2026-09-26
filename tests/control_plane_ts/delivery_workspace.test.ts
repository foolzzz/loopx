import assert from "node:assert/strict";
import test from "node:test";

import {
  DELIVERY_WORKSPACE_REQUEST_SCHEMA,
  evaluateDeliveryWorkspace,
  normalizeDeliveryWorkspaceSnapshot,
} from "../../loopx/control_plane/agents/delivery_workspace.ts";

test("builds typed git and local-goal workspace snapshots", () => {
  assert.deepEqual(evaluateDeliveryWorkspace({
    schema_version: DELIVERY_WORKSPACE_REQUEST_SCHEMA,
    operation: "build",
    observation: {
      workspace_identity: "git:GitHub.com/example/loopx.git",
      identity_kind: "git_repository",
      workspace_revision_digest:
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      repository_source: "current_git_origin",
      workspace_kind: "independent_git_worktree",
      peer_independent_worktree_required: true,
    },
  }), {
    schema_version: "loopx_delivery_workspace_result_v0",
    workspace: {
      schema_version: "delivery_workspace_v1",
      workspace_identity: "git:github.com/example/loopx",
      identity_kind: "git_repository",
      task_repository: "git:github.com/example/loopx",
      workspace_revision_digest:
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      repository_source: "current_git_origin",
      workspace_kind: "independent_git_worktree",
      peer_independent_worktree_required: true,
    },
  });

  assert.deepEqual(evaluateDeliveryWorkspace({
    schema_version: DELIVERY_WORKSPACE_REQUEST_SCHEMA,
    operation: "build",
    observation: {
      workspace_identity: "loopx:local-goal",
      identity_kind: "local_goal",
      repository_source: "goal_id_fallback",
      workspace_kind: "local_goal_workspace",
      peer_independent_worktree_required: false,
    },
  }), {
    schema_version: "loopx_delivery_workspace_result_v0",
    workspace: {
      schema_version: "delivery_workspace_v1",
      workspace_identity: "loopx:local-goal",
      identity_kind: "local_goal",
      task_repository: null,
      repository_source: "goal_id_fallback",
      workspace_kind: "local_goal_workspace",
      peer_independent_worktree_required: false,
    },
  });
});

test("normalizes legacy git snapshots without weakening peer policy", () => {
  assert.deepEqual(normalizeDeliveryWorkspaceSnapshot({
    schema_version: "delivery_workspace_v0",
    task_repository: "git:github.com/example/loopx",
    repository_source: "current_git_origin",
    workspace_kind: "canonical_checkout",
    peer_independent_worktree_required: true,
  }), {
    schema_version: "delivery_workspace_v1",
    workspace_identity: "git:github.com/example/loopx",
    identity_kind: "git_repository",
    task_repository: "git:github.com/example/loopx",
    repository_source: "current_git_origin",
    workspace_kind: "canonical_checkout",
    peer_independent_worktree_required: true,
  });
});

test("rejects local workspaces that claim peer-independent delivery", () => {
  assert.equal(normalizeDeliveryWorkspaceSnapshot({
    schema_version: "delivery_workspace_v1",
    workspace_identity: "loopx:local-goal",
    identity_kind: "local_goal",
    task_repository: null,
    repository_source: "goal_id_fallback",
    workspace_kind: "local_goal_workspace",
    peer_independent_worktree_required: true,
  }), null);
});

test("workspace normalization is immutable and fails closed on contradictions", () => {
  const candidate = {
    schema_version: "delivery_workspace_v1",
    workspace_identity: "git:github.com/example/loopx",
    identity_kind: "git_repository",
    task_repository: "git:github.com/example/other",
    repository_source: "current_git_origin",
    workspace_kind: "canonical_checkout",
    peer_independent_worktree_required: false,
  };
  const before = structuredClone(candidate);
  assert.equal(normalizeDeliveryWorkspaceSnapshot(candidate), null);
  assert.deepEqual(candidate, before);

  assert.throws(
    () => evaluateDeliveryWorkspace({
      schema_version: DELIVERY_WORKSPACE_REQUEST_SCHEMA,
      operation: "build",
      observation: {
        workspace_identity: "loopx:local-goal",
        identity_kind: "local_goal",
        repository_source: "goal_id_fallback",
        workspace_kind: "local_goal_workspace",
        peer_independent_worktree_required: "false",
      },
    }),
    /peer_independent_worktree_required must be a boolean/,
  );
});

// Fork decision 29: the Todo workspace identity (E2E pilot gaps G1 and G3).
const LOCAL_API = `local:${"a".repeat(64)}`;
const TODO_WORKSPACE = {
  goal_id: "g",
  todo_id: "todo_1",
  branch: "loopx/g/todo_1",
  repos: [
    { name: "api", path: "api", head_sha: "1".repeat(40), repo_id: LOCAL_API },
    {
      name: "web",
      path: "web",
      head_sha: "2".repeat(40),
      repo_id: "git:GitHub.com/example/web.git",
    },
  ],
};
const TODO_SNAPSHOT = {
  schema_version: "delivery_workspace_v1",
  workspace_identity: "todo-workspace:g/todo_1",
  identity_kind: "todo_workspace",
  task_repository: null,
  workspace_revision_digest: "b".repeat(64),
  repository_source: "todo_workspace_root",
  workspace_kind: "todo_workspace_root",
  peer_independent_worktree_required: true,
  todo_workspace: TODO_WORKSPACE,
};

test("builds and replays a multi-repo todo workspace snapshot", () => {
  const { schema_version: _schema, task_repository: _repo, ...observation } = TODO_SNAPSHOT;
  const built = evaluateDeliveryWorkspace({
    schema_version: DELIVERY_WORKSPACE_REQUEST_SCHEMA,
    operation: "build",
    observation,
  }).workspace;
  const expected = structuredClone(TODO_SNAPSHOT);
  expected.todo_workspace.repos[1].repo_id = "git:github.com/example/web";
  assert.deepEqual(built, expected);
  assert.deepEqual(normalizeDeliveryWorkspaceSnapshot(built), expected);
});

test("a local-only repository delivers under its local repo id", () => {
  assert.deepEqual(normalizeDeliveryWorkspaceSnapshot({
    schema_version: "delivery_workspace_v1",
    workspace_identity: LOCAL_API,
    identity_kind: "git_repository",
    task_repository: LOCAL_API,
    repository_source: "current_git_common_dir",
    workspace_kind: "independent_git_worktree",
    peer_independent_worktree_required: true,
  }), {
    schema_version: "delivery_workspace_v1",
    workspace_identity: LOCAL_API,
    identity_kind: "git_repository",
    task_repository: LOCAL_API,
    repository_source: "current_git_common_dir",
    workspace_kind: "independent_git_worktree",
    peer_independent_worktree_required: true,
  });
  for (const identity of ["local:abc", `local:${"A".repeat(64)}`, "/tmp/api"]) {
    assert.equal(normalizeDeliveryWorkspaceSnapshot({
      schema_version: "delivery_workspace_v1",
      workspace_identity: identity,
      identity_kind: "git_repository",
      task_repository: null,
      repository_source: "current_git_common_dir",
      workspace_kind: "independent_git_worktree",
      peer_independent_worktree_required: true,
    }), null);
  }
});

test("todo workspace snapshots fail closed on spoofed or unsafe fields", () => {
  const variants: Array<(snapshot: typeof TODO_SNAPSHOT) => void> = [
    (s) => { s.workspace_identity = "todo-workspace:g/todo_2"; },
    (s) => { s.todo_workspace.branch = "main"; },
    (s) => { s.todo_workspace.todo_id = "../x"; },
    (s) => { s.workspace_kind = "independent_git_worktree"; },
    (s) => { s.todo_workspace.repos[0].path = "/abs/api"; },
    (s) => { s.todo_workspace.repos[0].path = "../api"; },
    (s) => { s.todo_workspace.repos[0].head_sha = "main"; },
    (s) => { s.todo_workspace.repos[0].repo_id = "/abs/api"; },
    (s) => { s.todo_workspace.repos[1].name = "api"; },
    (s) => { s.todo_workspace.repos = []; },
    (s) => { s.task_repository = LOCAL_API as unknown as null; },
    (s) => { delete (s as Partial<typeof TODO_SNAPSHOT>).todo_workspace; },
  ];
  for (const mutate of variants) {
    const candidate = structuredClone(TODO_SNAPSHOT);
    mutate(candidate);
    assert.equal(normalizeDeliveryWorkspaceSnapshot(candidate), null, JSON.stringify(candidate));
  }
  // Only a todo_workspace snapshot carries the per-repo list.
  assert.equal(normalizeDeliveryWorkspaceSnapshot({
    schema_version: "delivery_workspace_v1",
    workspace_identity: "git:github.com/example/loopx",
    identity_kind: "git_repository",
    task_repository: null,
    repository_source: "current_git_origin",
    workspace_kind: "independent_git_worktree",
    peer_independent_worktree_required: true,
    todo_workspace: TODO_WORKSPACE,
  }), null);
});
