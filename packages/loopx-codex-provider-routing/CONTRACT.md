# Contract And Authority Boundary

Protocol: `loopx_codex_provider_routing_extension_v1`

Request: `loopx_codex_provider_routing_request_v1`

Response: `loopx_codex_provider_routing_response_v1`

Catalog: `codex_provider_routing_catalog_v1`

CLI plan: `codex_cli_route_launch_plan_v1`

Version 0.12.0 deliberately breaks the previous App-oriented wire. There are no
aliases for desktop patch, heartbeat transport or host-control recovery
operations. `qualify_snapshot` now means CLI configuration readback; it does not
accept durable App settings revisions, bundle observations or task UI facts.

## Closed public inputs and outputs

One invocation accepts exactly one supported operation with its matching
payload. Request and response schemas close all object shapes and runtime
validation uses those same schemas. Unknown keys, raw bodies, prompts, tokens,
credential-shaped material and private references fail before execution; the
CLI returns `invalid_request` without echoing caller-controlled values.
References are bounded public Git refs or opaque symbolic ids, never local paths,
private endpoints, environment values or shell commands. See
[`request.schema.json`](schemas/request.schema.json) and
[`response.schema.json`](schemas/response.schema.json) for exact enums and limits.

The extension has no filesystem, network, credential or Kernel transition
permission. It does not inspect HOME/CODEX_HOME, install CPA, rotate credentials,
start a turn or mutate sessions. Results are declarations and caller-observation
checks. Empty permission lists are an application boundary, **not an OS sandbox**.
A trusted launcher must separately keep credential variables out of the managed
extension environment; this protocol grants no authority to read them.

## CLI launch declaration

`compile_cli_plan` requires a `cli_plan` containing:

- `catalog_source`: the closed public catalog source;
- `route_id`, `routing_revision`, `deployment_ref`: opaque logical ids;
- `provider_id`: the Codex endpoint provider id, distinct from CPA account slots;
- `model_selector`, `reasoning_effort`, `service_tier`;
- `required_capabilities`: `modalities` (`text`, `image`) and `tool_transport`
  (`function_call`, `custom_tool_call`);
- `codex_version_requirement`: exactly `0.160.0`.

The compiler normalizes the original selector before alias mapping, checks the
reasoning declaration and filters candidates by all required modalities,
effective-priority/Fast and tool transport. `fast/` forces `priority`; an ordinary
selector already requesting `priority` receives the same Fast-capable admission.
Missing custom-tool declarations are function-only. Unsupported candidates fail
closed. CLI plans currently require native eligible candidates; heterogeneous
online history qualification has not been completed.

The result contains the fixed ids and version requirement, normalized service
tier, eligible candidates, an empty `provider_overrides`,
`qualification: declaration_only` and `online_qualification: held`. It contains
no base URL, executable/catalog path, key value, arbitrary `-c` dictionary,
approval/sandbox policy, MCP or hook settings. The trusted operator supplies
local endpoint and env-key names outside this public wire. There is no LoopX
Turn/Chat plan consumer in this version; a plan alone is not a launched binding.

## Catalog and attempt semantics

The catalog describes one bounded account ring. Prefer selectors change its
entrypoint; affinity may reorder only eligible members. A terminal fallback is
not a ring member and must not be revisited. Catalog declarations are not
entitlements. Every online CPA attempt must independently check complete history,
image capability, effective priority, custom tools and provider-bound context.
Do not strip images, downgrade tools or discard history to make fallback fit.

CPA is the sole online routing owner. Failover is permitted only before the
first committed generated content/tool item. After a text delta or tool call,
return a typed failure and recover through the owning session/effect boundary.
A CLI exit code is not proof that replay or another provider is safe.

`project_runtime_status` keeps retained host identity separate from route intent
and actual caller-observed CPA attempts. A direct hit on a later eligible account
is not fallback; fallback requires more than one attempted candidate. Slot ids
are symbolic. Account emails, auth filenames, tokens, raw management bodies,
request contents and private session paths are rejected.

## CLI configuration qualification

`qualify_snapshot` accepts exactly `codex_cli_version`,
`profile_config_parsed`, `catalog_readback_matches` and
`provider_readback_matches`. A pass requires CLI `0.160.0` and all three boolean
readbacks. Its result is `codex_cli_configuration_qualification_v1`, scoped to
`offline_configuration_only`, with `online_qualification: held`.

Successful TOML parsing or `app-server model/list` proves neither an actual
selected subscription, account entitlement, model response, session continuity
nor online failover. Prior App qualification is not a CLI deployment receipt.

## Recovery observation contracts

`qualify_quota_recovery` orders a successful reset after the source observation
of an old cooldown. A newer reset requires cooldown invalidation and a bounded
probe of that same account before fallback; only a fresh quota-limited probe
permits replacement cooling.

`qualify_outage_recovery` likewise requires a recovery signal newer than the
incident cooldown source, bounded reprobe and clearing or capability-revalidating
a degraded fallback binding. A text fallback success does not prove native
image/Fast/custom-tool readiness.

`qualify_tool_transport` compares required and observed item types and requires
completed host dispatch. `custom_tool_call` cannot be silently converted to
`function_call {"input": ...}`. HTTP success alone cannot pass.

`qualify_stream_recovery` checks an observed `sse_idle_timeout`, previous and
effective deadlines/retries, incremental small-event delivery before EOF and a
terminal event from upstream. The effective deadline must exceed the observed
silent gap; retries must not increase. Verify both text and a tool round trip in
the original session/home with preserved history. A fresh session, synthetic
terminal or HTTP 200 is insufficient. Fixture timings are not product defaults.

These operations inspect only caller-supplied content-free facts. They do not
probe a provider, reset cooldowns, replay tools or modify host configuration.
Their JSON claims are unauthenticated; a qualified observation cannot stand in
for the separate deployment acceptance matrix. Tool effect uncertainty must be
held by its owner, not replayed because the session resumed successfully.

## Upgrade and lifecycle

`reconcile_integration_candidate` checks ordered public source refs, exact heads,
required seams and last-sync observations. Drift produces `sync_required`; no
fetch, merge, push or deploy is performed. `upgrade_plan` returns checks and a
rollback trigger. Installation and process control belong to the separately
opted-in local operator.

Activate a pure observation after installing/registering the extension:

```sh
loopx extension run loopx-codex-provider-routing \
  --input-json packages/loopx-codex-provider-routing/examples/stream-recovery.json \
  --execute --format json
```

Read `result.qualified` and the operation's checks/failure codes. To disable,
stop submitting the operation or run
`loopx extension disable loopx-codex-provider-routing --execute --format json`.
Read back disabled state with `loopx extension list --format json`; optional
package removal is `python3 -m pip uninstall loopx-codex-provider-routing`. Pure operations create no runtime to roll back. Operator rollback
restores only allowlisted artifacts and routing metadata while preserving current
OAuth refresh tokens, auth/session stores and independently owned services.
See [OPERATOR.md](OPERATOR.md) and [RUNBOOK.md](RUNBOOK.md).
