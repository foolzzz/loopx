import assert from "node:assert/strict";
import {
  agentFamily,
  presentedAgentFamily,
} from "../../../node_modules/.cache/loopx-agent-family/agent-family.js";

// Positive: every documented shape still resolves to its host family, so a
// built-in Endpoint keeps its label.
assert.equal(agentFamily("codex"), "codex");
assert.equal(agentFamily("codex-cli"), "codex");
assert.equal(agentFamily("codex_cli"), "codex");
assert.equal(agentFamily("Codex-Worker-1"), "codex");
assert.equal(agentFamily("codex-main-control"), "codex");
assert.equal(agentFamily("claude-code"), "claude");

// Negative: an unrelated operator id that merely begins with a family root
// keeps its own identity, matching the backend. Displaying it as the host would
// attribute another agent's work to Codex/Claude.
assert.equal(agentFamily("codexplorer"), "codexplorer");
assert.equal(agentFamily("claudeflow"), "claudeflow");

// The typed adapter kind wins over the operator-chosen id when present, and the
// generic transport/projection placeholders are not treated as family signals.
assert.equal(presentedAgentFamily("codexplorer", "codex-cli"), "codex");
assert.equal(presentedAgentFamily("codexplorer", null), "codexplorer");
assert.equal(presentedAgentFamily("codexplorer", ""), "codexplorer");
assert.equal(
  presentedAgentFamily("codexplorer", "status_projection"),
  "codexplorer",
);
assert.equal(presentedAgentFamily("codex-cli", "acp"), "codex");
assert.equal(presentedAgentFamily("custom-worker", "acp"), "custom-worker");

console.log("Agent family presentation invariants passed");
