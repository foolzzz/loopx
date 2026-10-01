# Codex App entry retirement and migration

LoopX has retired Codex App onboarding, activation, and automation integration, including the former SSH
surface. This page keeps the old chapter path so bookmarks and internal links lead to the current supported
entrypoint. It no longer provides App installation, SSH, or heartbeat instructions.

## Use Codex CLI for ordinary projects

For local projects, continue with [Start from Codex CLI](./07-codex-cli.md). Codex CLI is the current
default visible, interruptible entrypoint and uses the same project Goal, Todo, Gate, evidence, and quota
contracts.

Existing project state does not need to be migrated or recreated. Before switching, verify that the
checkout, registry, `goal_id`, and `agent_id` still identify the original work, and that no other executor
holds the same Todo claim or lease:

```bash
loopx status --goal-id <goal-id>
loopx history --goal-id <goal-id> --limit 10
```

For an unconnected project, follow the guided start in the CLI chapter. Do not restore old App automation,
activation commands, or App-specific scheduler configuration from earlier documentation.

## Mapping older instructions

| Older instruction | Current action |
| --- | --- |
| Connect a local project in the App | Use Codex CLI guided start |
| Install or activate App automation | Retired; do not restore it |
| Read cadence from the App scheduler | Retired; use the visible Codex CLI |
| Switch between App and CLI | Move to Codex CLI and handle identity and leases explicitly |
| Work through the former App SSH path | Retired; use Codex CLI |

Treat any remaining local App activation, automation, or SSH steps as documentation drift. Use the current
Codex CLI chapter and `--help` instead.
