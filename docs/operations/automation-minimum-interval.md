# Minimum local schedule interval

This opt-in policy keeps LoopX's **local scheduler recommendations** at or
above an owner-configured minimum, including after backoff/reset. It does not
intercept timer execution or enforce a token budget. Existing schedulers are
not changed by configuration alone.

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

For an already bound turn, inspect its `quota should-run` scheduler packet
using that turn's normal identity and selection contract. Do not create a new
turn solely to observe a timer. The typed `scheduler_hint.reset_policy` exposes
the local initial interval and reset token; opt-in cold detail exposes the full
`local_scheduler` progression. The owning local scheduler applies that policy
and performs its own readback. LoopX does not project or mutate host-specific
automation schedules.

If the local scheduler rejects the required interval, hold that scheduler and
retain the failure; do not shorten the floor or activate an alternate scheduler.

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
frontend/Lark editor is added: this delivery uses the existing local scheduler
plus CLI configuration path. Settings projection is tracked in
[RFC M3](../architecture/rfcs/automatic-execution-admission-v0.md).

The App-bundled runtime supports prompt hooks in an isolated test, but actual
App automatic-trigger coverage, hook trust/failure handling and reliable manual
intent remain unqualified. `pre_model_atomic_admission: not_qualified` is not a
claim that the host has no hook mechanism. See the RFC's research ledger.
