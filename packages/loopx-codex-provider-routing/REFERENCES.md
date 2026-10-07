# Module And Configuration References

Version 0.12.0 replaces App automation and bundle integration with an optional,
manual Codex CLI configuration outlet. It introduces no new built-in capability,
Turn scheduler or online proxy.

## Responsibility inventory

| Responsibility | Owner and boundary |
| --- | --- |
| Closed public wire validation | `schema_contract.py` and `schemas/`; runtime and schemas reject unknown fields |
| Secret-free ring, selector and CLI plan compilation | `contract.py`; symbolic declarations, no deployment effects |
| Model metadata normalization | `model_metadata.py`; each upstream model retains its own context/reasoning/capability metadata |
| CLI catalog/profile generation and isolated model/list probe | `operator_catalog.py::CLIModelCatalog`; explicit local references |
| Explicit local settings and target validation | `operator_settings.py`; v2 private configuration, allowlisted targets |
| CPA credentials, process and safe observation reduction | `operator_runtime.py` and `operator_observations.py`; separately invoked operator |
| Snapshot/rollback and profile installation | `operator.py`; checked fixed targets, latest tokens retained |
| Quota, outage, stream and tool qualification | Pure caller-observation operations; CPA/host effects stay outside the extension |
| Public source drift and upgrade planning | `reconcile_integration_candidate` / `upgrade_plan`; Git and installation effects are excluded |
| Online routing, retry, commit barrier, history adaptation | CPA only; no LoopX data-plane copy |
| Codex permission settings and session store | Codex/host owner; no profile-driven permission escalation or store migration |

The metadata extraction is a bounded refactor of the previous App catalog owner,
so CLI consumers do not acquire a second model-rule implementation. Archived App
live conclusions and desktop/heartbeat operations confer no CLI qualification.

## Public fixtures

- [`examples/request.json`](examples/request.json): catalog source;
- [`examples/cli-plan.json`](examples/cli-plan.json): pinned symbolic CLI plan;
- [`examples/normalize-request.json`](examples/normalize-request.json): Fast/tier/tool admission;
- [`examples/qualification-snapshot.json`](examples/qualification-snapshot.json): offline CLI configuration readback;
- [`examples/runtime-status.json`](examples/runtime-status.json): symbolic route/quota/activity observation;
- [`examples/quota-recovery.json`](examples/quota-recovery.json),
  [`examples/outage-recovery.json`](examples/outage-recovery.json),
  [`examples/stream-recovery.json`](examples/stream-recovery.json) and
  [`examples/tool-transport.json`](examples/tool-transport.json): content-free recovery observations;
- [`examples/integration-candidate.json`](examples/integration-candidate.json) and
  [`examples/upgrade-request.json`](examples/upgrade-request.json): public-safe exact-source and changed-seam plans.

[`templates/model-metadata.synthetic.json`](templates/model-metadata.synthetic.json)
is a versioned offline metadata fixture;
[`templates/cpa-native.config.toml`](templates/cpa-native.config.toml) shows the
standalone CLI profile shape. The adapter's fixed public instruction placeholder
is for CLI parsing only. Real model behavior needs an owner-selected trusted
metadata/instruction source and separate online qualification.

Generated profiles are standalone `<name>.config.toml` files for the pinned CLI
layout. Public templates contain placeholders, not credential-bearing deployment
configuration. Private operator configuration, credentials, binary pins, raw
metadata, logs, request bodies and snapshots remain outside the public repository.
Do not inherit App retry defaults or turn historic observations into new evidence.

## Upstream references and evidence limits

The CLI profile/config layout targets **Codex CLI 0.160.0**; verify the generated
profile with that installed version in an isolated HOME/CODEX_HOME. Primary
references are [Codex CLI](https://developers.openai.com/codex/cli),
[configuration reference](https://developers.openai.com/codex/config-reference)
and [Codex source](https://github.com/openai/codex). Online documentation can
change; local versioned CLI parsing is the acceptance evidence for this outlet.

[CLIProxyAPI source](https://github.com/router-for-me/CLIProxyAPI) owns the online
data plane. A source link or catalog declaration is not proof that a particular
CPA binary implements image/history/Fast/custom-tool admission or the commit
barrier. Lock a build and qualify it independently. No historical upstream PR
status or App self-use deployment is promoted into a current CLI support claim.

[OPERATOR.md](OPERATOR.md) owns runnable local activation/readback/rollback;
[CONTRACT.md](CONTRACT.md) owns the closed wire and authority boundary;
[RUNBOOK.md](RUNBOOK.md) owns deployment acceptance and held online work.
