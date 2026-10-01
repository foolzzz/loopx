# Codex App minimum schedule interval

This opt-in policy keeps LoopX's **Codex App schedule recommendations** at or
above an owner-configured minimum, including after backoff/reset. It does not
intercept App timer execution or enforce a token budget. Existing automations
are not changed by configuration alone.

## Configure and apply

Pause the affected automatic schedule while changing its policy. Use the same
registry and runtime root as its LoopX heartbeat. Read its current policy:

```sh
loopx --format json automation-cadence --goal-id my-goal --agent-id my-agent
```

Configure at the Goal level by omitting `--agent-id`, at the agent level as
below, or add `--automation-id my-automation` for one existing automation. Parent
and child constraints combine by maximum; an agent rule covers all of its
launch mechanisms' schedule projections. Use the returned configuration revision
instead of assuming it is zero:

```sh
loopx --format json automation-cadence --goal-id my-goal --agent-id my-agent \
  --min-interval-minutes 1440 --expected-revision 0 \
  --owner-reference owner-daily-cadence
```

Review the preview, then repeat with `--execute`. Inspect it again in a separate
command to confirm the saved revision and contributing sources. Do not infer
owner approval from a scheduler recommendation. A local owner-reference records
caller intent, not authentication against other processes sharing the OS user.

For an already bound App turn, inspect its `quota should-run` scheduler packet
using that turn's normal identity and selection contract. Do not create a new
turn solely to observe a timer. `scheduler_hint.app_automation` and its legacy
`codex_app` projection expose the floor, desired RRULE, stateless
`stateful_backoff.apply_needed` decision and `guarantee`. There is no scheduler
ACK command or persisted per-App apply state. When `apply_needed=true`, apply the
desired schedule once with the App's `automation_update` tool while preserving
task binding, prompt, status and notification preference. Then view that same
automation and compare its actual schedule. Pass the observed RRULE to the next
normal `quota should-run` call with `--app-automation-current-rrule`; an exact
match produces `apply_needed=false` and skips a no-op update. A configuration
write or update-tool response alone is not actual schedule readback. Resume only
the affected schedule when its normal activation requirements are met.

If the App rejects the required interval, hold that automation and retain the
failure; do not shorten the floor or activate an alternate scheduler. A daily
wall-clock schedule and 1440 elapsed minutes can differ around timezone/DST
changes; this policy uses elapsed minutes and projects a minute-based RRULE.

## Disable or roll back

Lowering or disabling a scope needs an explicit owner decision and the latest
revision. Zero disables only this scope; parent constraints still apply:

```sh
loopx --format json automation-cadence --goal-id my-goal --agent-id my-agent \
  --min-interval-minutes 0 --expected-revision 1 \
  --owner-reference owner-disable-daily-cadence --approve-reduction --execute
```

Read back the effective policy. Keep the schedule paused before downgrading to
an older LoopX version that ignores this policy. Do not delete policy files or
reset backoff history to bypass an interval. No policy is enabled by default.

## What this version verifies

CLI configuration/readback, inherited constraints, version conflicts and
recommendation/reset parity are covered by real temporary-file tests. No
frontend/Lark editor is added: this first delivery is explicitly the existing
App automation plus CLI configuration path. Settings projection is tracked in
[RFC M3](../architecture/rfcs/automatic-execution-admission-v0.md).

The App-bundled runtime supports prompt hooks in an isolated test, but actual
App automatic-trigger coverage, hook trust/failure handling and reliable manual
intent remain unqualified. `pre_model_atomic_admission: not_qualified` is not a
claim that the host has no hook mechanism. See the RFC's research ledger.
