import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  VISION_REFRESH_REQUEST_SCHEMA,
  buildVisionCheckpoint,
} from "../../loopx/control_plane/goals/vision_checkpoint.ts";

// The Python owners (loopx/public_safe_text.py) and this TypeScript owner must
// agree on the same corpus. The Python parity test drives this file through
// refresh-state; this test pins the TypeScript owner on its own so the
// contract cannot drift when only the control-plane suite runs.
const CORPUS_PATH = fileURLToPath(
  new URL("../fixtures/public_safe_text_corpus.json", import.meta.url),
);

interface CorpusSample {
  id: string;
  template: string;
}

interface Corpus {
  schema_version: string;
  tokens: Record<string, string[]>;
  public_safe: CorpusSample[];
  private_looking: CorpusSample[];
}

const corpus = JSON.parse(readFileSync(CORPUS_PATH, "utf8")) as Corpus;
assert.equal(corpus.schema_version, "public_safe_text_corpus_v0");

function render(template: string): string {
  return template.replace(/\{([A-Z][A-Z_]*)\}/g, (_match, name: string) => {
    const direct = corpus.tokens[name];
    if (direct !== undefined) return direct.join("");
    const lowerSuffix = "_LOWER";
    if (name.endsWith(lowerSuffix)) {
      const base = corpus.tokens[name.slice(0, -lowerSuffix.length)];
      if (base !== undefined) return base.join("").toLowerCase();
    }
    throw new Error(`corpus placeholder ${name} has no token definition`);
  });
}

function checkpoint(text: string): void {
  buildVisionCheckpoint({
    schema_version: VISION_REFRESH_REQUEST_SCHEMA,
    phase: "finalize",
    agent_id: "fixture-cli",
    agent_vision: null,
    existing_agent_vision: null,
    vision_unchanged_reason: text,
    delivery_outcome: "outcome_progress",
    active_state_next_action_would_update: false,
    delivery_boundary: null,
    todo_id: null,
    completion_todo_id: null,
    autonomous_replan_recorded: false,
  });
}

test("vision checkpoint accepts every public-safe corpus sample", () => {
  for (const sample of corpus.public_safe) {
    assert.doesNotThrow(() => checkpoint(render(sample.template)), sample.id);
  }
});

test("vision checkpoint rejects every private-looking corpus sample", () => {
  for (const sample of corpus.private_looking) {
    assert.throws(
      () => checkpoint(render(sample.template)),
      /private-looking value/,
      sample.id,
    );
  }
});
