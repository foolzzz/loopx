# LoopX Codex Provider Routing

This optional extension compiles public-safe Codex CLI route declarations and
checks caller-supplied observations. CPA owns subscription selection and online
routing. A separately invoked local operator generates and explicitly installs
an independent CLI profile; the managed extension does not write files, open
network connections, read credentials or launch Codex.

Version 0.12.0 uses the breaking v1 request/response protocol. It removes the
retired App bundle, desktop patch, heartbeat and host-control operations without
compatibility aliases. It does not connect route plans to LoopX Turn, Chat,
Dashboard or Lark. Manual CLI configuration is the shipped entrypoint; real CPA
request acceptance is **held** pending an authorized deployment and request
budget. Offline readback does not establish account entitlement or failover.

## Placement and authority

The provider id is `loopx-codex-provider-routing`, an optional extension-delivered
package, not a new built-in capability. Existing LoopX extension lifecycle owns
registration; Codex CLI owns its sessions and permission settings; CPA owns
credentials, online attempts and the output commit barrier; the operator owns
explicit local artifacts and deployment pins.

`permissions = []` describes the application contract. It is **not an OS
sandbox** against code running as the same operating-system user. Supply only
public-safe inputs and invoke the managed runtime without credential-bearing
environment variables. Profile installation does not grant sandbox, approval,
MCP, hook, credential or Kernel transition authority.

## Managed usage

Use the same Python environment for package installation and LoopX:

```sh
python3 -m pip install packages/loopx-codex-provider-routing
loopx extension install \
  --manifest packages/loopx-codex-provider-routing/extension.toml \
  --execute --format json
loopx extension doctor loopx-codex-provider-routing --execute --format json
loopx extension run loopx-codex-provider-routing \
  --input-json packages/loopx-codex-provider-routing/examples/cli-plan.json \
  --execute --format json
```

Read `result.schema_version`, `result.qualification` and
`result.online_qualification`: a launch declaration is `declaration_only` with
online qualification `held`. It contains symbolic route/deployment references,
model/effort/tier and capability requirements; it contains no endpoint, path,
environment value or arbitrary permission override. The trusted operator resolves
local deployment details. See [the contract](CONTRACT.md).

## Operations

| Operation | Result and boundary |
| --- | --- |
| `compile_catalog` | Declared profiles, a bounded account ring and logical selectors; no online eligibility proof |
| `compile_cli_plan` | Native eligible candidates in `codex_cli_route_launch_plan_v1`, fixed route revision and deployment reference |
| `normalize_selector_request` | Original selector normalization and modality/Fast/tool admission before alias mapping |
| `qualify_snapshot` | Codex CLI 0.160.0 profile/catalog/provider configuration readback only |
| `project_runtime_status` | Symbolic route intent, caller-observed attempt chain and bounded quota/activity |
| `qualify_quota_recovery` | Reset/cooldown ordering and observed bounded reprobe |
| `qualify_outage_recovery` | Incident/cooldown ordering and observed degraded-affinity revalidation |
| `qualify_stream_recovery` | Observed deadline, incremental stream and original-session continuity |
| `qualify_tool_transport` | Observed item-type preservation and completed dispatch |
| `reconcile_integration_candidate` | Public exact-source drift plan; no Git or deployment effects |
| `upgrade_plan` | Bounded changed-seam checks and rollback checklist; no installation |

Recovery qualification is pure caller observation. The JSON contract cannot
authenticate supplied facts, repair CPA, resume a session or establish arbitrary
tool exactly-once execution. `ok=true` means a valid response; inspect each
operation's qualification checks separately.

## Manual CLI profile

The opt-in [local operator](OPERATOR.md) writes a standalone
`<name>.config.toml`, using the CLI 0.160.0 profile layout. It does not write
legacy `[profiles]` sections or the user's `config.toml`, `auth.json`, session
store or rollouts. Installation requires an explicit `paths.codex_home` and
`--execute install-profile`; configuration generation alone does not activate it.

Select it for manual CLI/exec commands with `codex --profile NAME`.
CLI 0.160.0 `app-server` does not accept `--profile`; the operator's offline
probe passes the same bounded configuration as `-c` overrides for readback.
Stop passing the profile option to return to ordinary CLI selection. Disable the managed extension explicitly;
this leaves its registration and operator artifacts in place and does not stop
an independent CPA process:

```sh
loopx extension disable loopx-codex-provider-routing --execute --format json
loopx extension list --format json
# Optional package removal from the dedicated Python environment after disabling:
python3 -m pip uninstall loopx-codex-provider-routing
```

Restore generated profile/catalog artifacts with the operator's checked snapshot
rollback. Preserve current credentials and sessions.

## Validation

Run from the repository root:

```sh
uv run --extra test python packages/loopx-codex-provider-routing/smoke/codex_provider_routing_smoke.py
uv run --extra test python packages/loopx-codex-provider-routing/smoke/schema_contract_smoke.py
uv run --extra test python packages/loopx-codex-provider-routing/smoke/cli_plan_smoke.py
# Manual real-CLI acceptance (requires Codex CLI 0.160.0, sends no model request):
uv run --extra test python packages/loopx-codex-provider-routing/smoke/cli_profile_readback_smoke.py
uv run --extra test python packages/loopx-codex-provider-routing/smoke/integration_contract_smoke.py
uv run --extra test python packages/loopx-codex-provider-routing/smoke/recovery_contracts_smoke.py
uv run --extra test python packages/loopx-codex-provider-routing/smoke/operator_smoke.py
uv run --extra test python -m pytest -q tests/extensions/test_colocated_extension_layout.py
uv run --extra test loopx check --scan-path packages/loopx-codex-provider-routing
```

Tests use synthetic operator state and isolated homes. The independent profile
is parsed with `codex --profile NAME debug prompt-input`; its
prompt output is discarded. Separate `app-server model/list` and `config/read`
use bounded `-c` overrides. These offline checks do not send a model request.
Online text/tool/session,
account failover, image/Fast/custom-tool and pre/post-commit CPA acceptance remain
held. [RUNBOOK.md](RUNBOOK.md) owns that matrix; [REFERENCES.md](REFERENCES.md)
records the module and source boundaries.
