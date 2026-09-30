# Protocol Action Packet Codex CLI Wrapper v0

This wrapper is the cold-path bridge between `protocol_action_packet_v0` and a
future Codex CLI summarizer. It is intentionally outside `quota should-run` so
the hot path keeps its interface budget.

The wrapper consumes a synthetic `protocol_router_comparison_v0` report, not a
legacy field from fresh quota output. The [PR-05 migration](protocol-action-packet-decision-v0.md)
keeps it outside the live execution contract. It builds a Codex CLI command
envelope for an isolated project:

```text
codex exec --skip-git-repo-check --ephemeral --ignore-user-config
  --ignore-rules -c 'approval_policy="never"' --sandbox workspace-write
  -C <isolated-fixture-project> <public-safe prompt>
```

No LoopX code ships this wrapper. The fake-executable smoke that pinned the
command shape only exercised its own fixture and was retired, so this page is a
design note, not a supported command.

The wrapper may become a real Codex CLI cold-path experiment only when the
caller explicitly opts into real execution and keeps the output as a compact
sidecar. Direct LLM API wiring remains deferred until the Codex CLI wrapper or
another cold-path comparison proves a measurable action-clarity gain.
