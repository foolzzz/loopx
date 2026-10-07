# Local CPA operator

`loopx-cpa-operator` is a separately invoked, explicit local CLI. Managed extension
installation or execution does not activate it, start CPA, enroll an account,
read credentials or install a profile. Version 0.12.0 uses private settings v2
and standalone Codex CLI 0.160.0 profiles; App caches, bundles, automation and
legacy `[profiles]` configuration are outside its targets.

The manual outlet is **partial** until real CPA text/tool/session requests are
accepted against an authorized, pinned deployment. Online acceptance remains
**held** pending deployment information and a request budget. TOML parsing,
metadata and `model/list` do not establish entitlement or account failover.

## Explicit configuration

Install into a dedicated Python 3.11+ environment and supply a private regular
mode-0600 JSON file outside Git. The settings object is closed:

| Required field | Meaning |
| --- | --- |
| `schema_version` | `loopx_cpa_local_operator_v2` |
| `paths.runtime_root`, `paths.temporary_root` | Disjoint dedicated writable roots outside Git and Codex homes |
| `paths.binary`, `paths.codex_binary` | Explicit reviewed CPA and Codex executable references |
| `paths.model_metadata`, `paths.route_plan` | Versioned metadata and compiled public launch plan files |
| `binary_sha256`, `source_commit` | Exact reviewed CPA artifact pins |
| `port`, `launchd_label` | Owned unprivileged loopback port and service id |
| `profile_name` | Independent symbolic profile name; `config` and `auth` are rejected |
| `cpa_client_env_key` | Environment variable **name**, never a secret value |
| `fallback_routes` | Explicit selector opt-ins; use `[]` for native-only |

Optional paths are `login_source`, `plugin_directory`, `ark_env_file` and
`codex_home`. Optional values are `plugin_sha256`, `ark_base_url`, `ark_model` and
`ark_pro_model`. Plugin directory/digest must be supplied together. A nonempty
fallback list requires the Ark endpoint/model values and explicit env-file
reference. Native-only configuration requires no Ark key, cache or plugin.
Heterogeneous CLI launch qualification is still held; these optional operator
fields do not bypass the native-only launch-plan boundary.

Paths must be absolute and cannot traverse symlinks. Runtime and temporary roots
cannot overlap each other, Codex homes, Git worktrees or protected host stores.
Slots resolve to validated basenames within the owned auth directory. Receipts
remain symbolic; private paths, credentials, logs, snapshots, metadata and binary
artifacts stay operator-owned and outside the public repository.

`paths.route_plan` points to the complete v1 `compile_cli_plan` request envelope
(`schema_version`, `operation`, `cli_plan`), such as
[`examples/cli-plan.json`](examples/cli-plan.json) copied into the private owner
directory. The operator validates and compiles it to
`codex_cli_route_launch_plan_v1`, pinning route/revision/deployment,
provider/model, effort/tier and capabilities. It has no endpoint, path or key. The operator rejects candidate/tier declarations that differ from its fixed
A/B/C routing preset before writing any artifact. It also cross-checks the selected
model's effort and modalities against the explicit metadata. The operator
resolves its owned loopback endpoint and env-key name from trusted local settings.

`paths.model_metadata` points to `codex_cli_model_metadata_v1` with
`codex_cli_version: "0.160.0"`, `source_kind: "synthetic"` or
`"operator_verified"`, and bounded `models`. Each model retains its own declared
context, reasoning, modality and catalog metadata; one model does not inherit
another model's properties. Prompt/instruction fields and arbitrary cache bodies
are excluded. The public
[`model-metadata.synthetic.json`](templates/model-metadata.synthetic.json) fixture
proves configuration shape only. The catalog adapter supplies fixed public
placeholder instructions required by CLI 0.160.0 parsing, not a qualified model
prompt. Before real use, the owner must select the trusted locked metadata and
official instruction source. This version does not claim model-behavior
qualification; `operator_verified` metadata remains a caller declaration, not
online entitlement.

## Dry-run, generation and install

Run these commands with your explicitly prepared private configuration:

```sh
python3 -m pip install packages/loopx-codex-provider-routing
OPERATOR_CONFIG=/absolute/private/operator.json

# Default dry-run: reads/validates the named settings and emits a safe plan.
loopx-cpa-operator --config "$OPERATOR_CONFIG" validate
loopx-cpa-operator --config "$OPERATOR_CONFIG" write-profile

# Generate catalog and runtime/profiles/NAME.config.toml, retaining a snapshot.
loopx-cpa-operator --config "$OPERATOR_CONFIG" --execute write-profile

# Requires paths.codex_home; writes only CODEX_HOME/NAME.config.toml.
loopx-cpa-operator --config "$OPERATOR_CONFIG" install-profile
loopx-cpa-operator --config "$OPERATOR_CONFIG" --execute install-profile

# Real CLI parsing/model-list readback in an isolated temporary home.
loopx-cpa-operator --config "$OPERATOR_CONFIG" --execute probe
```

Without `--execute`, commands perform no writes, network requests or process
operations. `--execute` authorizes only the named local invocation. Profile
commands snapshot their fixed targets before writing. Installation refuses an
existing profile whose content differs; retain that file and choose a new name.

The generated independent file sets model/provider/catalog, reasoning effort,
service tier and the Responses endpoint/env-key reference. It does not set
approval, sandbox, hooks or MCP. No command writes the user's main
`config.toml`, `auth.json`, session database, rollouts or App bundle/automation.
This allowlist is an application contract, **not an OS sandbox**. Credentials
remain with the trusted operator/CPA/Codex owner; neither profile generation nor
managed extension permissions grant a plugin access to them.

`probe` checks CLI 0.160.0 and performs two separate offline checks in an
isolated home. It first parses the standalone profile with
`codex --profile NAME debug prompt-input`, discarding the prompt
output without saving it. CLI 0.160.0 `app-server` rejects `--profile`, so the
operator converts the same allowlisted profile fields to bounded `-c` arguments,
starts app-server with those overrides and compares `model/list` and `config/read`.
It does not start a thread or turn, make a model request or contact CPA. Inspect
its `passed`, selector counts and checks. This readback certifies configuration
only, even when all checks pass; it does not integrate the plan into Chat.

After separate deployment acceptance, select the independent profile explicitly
per CLI command with `codex --profile NAME`. The client key value must be injected
by the trusted caller into the named environment variable, never put in a public
request, generated profile or command-line argument. This package does not
automatically apply that profile to LoopX Turn, Chat, Dashboard or Lark.

## Separately authorized runtime commands

The operator retains explicit account/process operations. These may touch the
operator-owned credential store, local CPA process or management endpoint; they
are not part of offline CLI qualification. Dry-run first and use them only on the
owned deployment with authorization:

```sh
loopx-cpa-operator --config "$OPERATOR_CONFIG" enroll --slot c
loopx-cpa-operator --config "$OPERATOR_CONFIG" --execute enroll --slot c
loopx-cpa-operator --config "$OPERATOR_CONFIG" --execute reconcile
loopx-cpa-operator --config "$OPERATOR_CONFIG" --execute write-catalog
loopx-cpa-operator --config "$OPERATOR_CONFIG" --execute serve
loopx-cpa-operator --config "$OPERATOR_CONFIG" --execute status
loopx-cpa-operator --config "$OPERATOR_CONFIG" --execute route-status
```

Enrollment requires explicit `login_source`, valid identity/expiry, a vacant
symbolic slot and no duplicate account. The source remains untouched. CPA owns
later token refresh; do not concurrently rotate the same source login. `serve`
replaces the operator process with the pinned executable; the service manager
owns supervision. `start`/`stop` manage only its unmanaged process; `stop` refuses
an unrelated or launchd-managed process. The operator does not edit LaunchAgents.
Do not stop or reconfigure another client's shared CPA instance to validate this
outlet.

For a separately confirmed early quota recovery:

```sh
loopx-cpa-operator --config "$OPERATOR_CONFIG" reset-cooldown --slot b
loopx-cpa-operator --config "$OPERATOR_CONFIG" --execute reset-cooldown --slot b
loopx-cpa-operator --config "$OPERATOR_CONFIG" --execute route-status
```

This clears only the named enabled slot's cached CPA cooldown. It does not rotate
OAuth tokens, replenish quota or prove recovery. A bounded model request and
actual selection of that account are separate, budgeted acceptance; another
account succeeding does not qualify the target. No background recovery polling
is installed. CPA retains online admission, retry and the output commit barrier;
this package does not inherit historical App retry budgets or qualifications.

## Rollback and disable

Mutating profile/catalog/account commands emit a `rollback_snapshot` id. To
inspect the rollback plan and then restore it:

```sh
loopx-cpa-operator --config "$OPERATOR_CONFIG" rollback --snapshot-id SNAPSHOT_ID
loopx-cpa-operator --config "$OPERATOR_CONFIG" --execute rollback --snapshot-id SNAPSHOT_ID
```

Rollback validates integrity and every target before writes. Its allowlist is
owned slot metadata, registered credential routing fields, generated catalog,
the configured runtime profile and the explicitly configured installed profile.
Current OAuth refresh tokens are retained. Credential-routing rollback requires
the owned CPA process to be stopped before it can restore routing fields; it
refuses an active or unknown process state before writing. Profile/catalog-only
snapshots exclude credentials and slot state and do not require stopping CPA. Newly enrolled credentials remain
stored but are disabled when restoring the prior slot set. If a profile did not
exist before installation, rollback removes only the generated file whose digest
still matches; edited files are retained with an actionable failure. Main
config/auth and session stores are never rollback targets.

Stop passing `--profile NAME` to disable per-command opt-in. Roll back the
profile-install snapshot to remove an unchanged generated profile. Separately
unload the owned service or stop the unmanaged process to stop CPA; keep auth
state for recovery. Uninstalling the dedicated Python environment removes the
operator CLI. Disable managed invocation and read back its state explicitly:

```sh
loopx extension disable loopx-codex-provider-routing --execute --format json
loopx extension list --format json
python3 -m pip uninstall loopx-codex-provider-routing
```

Disable leaves the extension registration and operator data in place; package
removal does not stop an independent operator service or erase its data.

## Verification

```sh
uv run --extra test python packages/loopx-codex-provider-routing/smoke/operator_smoke.py
uv run --extra test python packages/loopx-codex-provider-routing/smoke/codex_provider_routing_smoke.py
uv run --extra test python packages/loopx-codex-provider-routing/smoke/cli_plan_smoke.py
```

Offline checks use synthetic credentials/state and temporary isolated homes.
Run the smokes for dry-run effects, native-only settings, independent profile
targets, versioned metadata, snapshot integrity and token-safe rollback.
Separately run `probe` with the real pinned CLI for parsing/model-list evidence;
unit or fake-server passes do not replace that check. The [runbook](RUNBOOK.md) keeps real CPA requests, pre/post-commit
failover, image/Fast/custom-tool and session continuity explicitly held.
