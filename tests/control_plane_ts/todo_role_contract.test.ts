import assert from "node:assert/strict";
import test from "node:test";
import { normalizeTodoRoleContract, planTodoFieldUpdate, TODO_FIELD_UPDATE_REQUEST_SCHEMA } from "../../loopx/control_plane/todos/field_update.ts";

test("role contract patch normalizes values and treats defaults as clears", () => {
  assert.deepEqual(normalizeTodoRoleContract({required_role: " Developer ", requires_acceptance: false,
    acceptor_agent: "codex-acceptor", reject_count: 0, task_repositories: ["api", "Web", "api"]}),
  {required_role: "developer", requires_acceptance: false, acceptor_agent: "codex-acceptor",
    reject_count: null, task_repositories: ["api", "Web"]});
  assert.deepEqual(normalizeTodoRoleContract({acceptor_agent: null, task_repositories: []}),
    {acceptor_agent: null, task_repositories: null});
});

test("role contract patch rejects unknown fields and invalid values", () => {
  for (const patch of [{role: "developer"}, {required_role: "boss"}, {requires_acceptance: "true"},
    {reject_count: -1}, {reject_count: 1.5}, {task_repositories: "api"}, {task_repositories: ["bad name"]}]) {
    assert.throws(() => normalizeTodoRoleContract(patch));
  }
});

test("field planner applies the role contract as metadata updates", () => {
  const plan = planTodoFieldUpdate({schema_version: TODO_FIELD_UPDATE_REQUEST_SCHEMA,
    todo: {todo_id: "todo_role1", status: "open", acceptor_agent: "codex-old"},
    intent: {role_contract: {reject_count: 2, acceptor_agent: null}}, updated_at: "2026-09-26T00:00:00Z"});
  const updates = plan.metadata_updates;
  assert.equal(updates.reject_count, 2);
  assert.equal(updates.acceptor_agent, null);
  assert.equal(Object.hasOwn(updates, "required_role"), false);
});
