# Status service readiness

The local status service exposes `GET /?readiness=1` for local service startup.
The existing root payload and runtime identity are returned with a
`loopx_status_readiness_v0` object containing `state` and `reason`.

| State | Reason | Meaning |
| --- | --- | --- |
| `ready` | `registry_readable` | Existing registry loading returns a JSON object. An absent registry remains a valid empty installation. |
| `failed` | `registry_invalid` | Invalid JSON, encoding, or a non-object root. |
| `failed` | `registry_unavailable` | An I/O error prevents registry loading. |

HTTP 200 means the probe was served, not that the component is ready. `ok`
retains its root-response meaning. Consumers must inspect the typed state.
The route accepts loopback browser origins only, returns no exception text or
registry paths, and makes no writes, Goal projections, provider calls or scans.
`/healthz` remains liveness-only; the ordinary root request does not read the
registry. The response is not cached and readiness is read again on retry.

The query distinguishes failed registry readiness from an unresponsive listener.
A readiness advertisement without a typed result is not readiness evidence;
legacy-service identity compatibility remains a separate client contract.

This is a bounded existing-service prerequisite for
[#3930](https://github.com/huangruiteng/loopx/issues/3930), not the unified daemon
contract or complete M1 delivery. It does not establish service-profile identity,
validate individual Goals, prove writable storage, qualify launchd lifecycle,
or assert Chat/provider readiness. Profile identity, supervisor composition and
migration remain separate implementation work.

To diagnose failure, repair the registry selected by the service and retry the
service connection. Existing CLI registry selection and authority are unchanged.
There is no daemon activation or persisted-state migration in this slice;
reverting the client/server changes restores the previous startup probe.
