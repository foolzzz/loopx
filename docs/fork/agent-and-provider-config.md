# Agent and provider config (fork, slice S3)

Implements design-v0 decisions 14-17 and the Turn-host seams. Code lives in
`loopx/agent_config/`; CLI in `loopx/cli_commands/agent_config.py`.

## Files

`<runtime-root>` defaults to `~/.codex/loopx` (or `--runtime-root` / the
registry's `common_runtime_root`).

### `<runtime-root>/providers.yaml`

```yaml
providers:
  anthropic-login:            # list form with `name:` is also accepted
    kind: anthropic           # anthropic | openai | openai-compatible | codex-cpa
    auth: {type: oauth_cli}   # uses `claude auth` / `codex login` (cli: claude|codex)
  anthropic-token:
    kind: anthropic
    auth: {type: oauth_token, env: CLAUDE_CODE_OAUTH_TOKEN}
  anthropic-key:
    kind: anthropic
    base_url: https://gateway.example   # optional, becomes ANTHROPIC_BASE_URL
    auth: {type: api_key, env: ANTHROPIC_API_KEY, keychain: {service: my-anthropic, account: me}}
  cpa:
    kind: codex-cpa           # defaults: base_url http://127.0.0.1:8317/v1, wire_api responses,
    auth: {type: api_key}     # model_provider_id cpa, display_name CPA, env CPA_API_KEY, service_tier default
  local:
    kind: openai-compatible
    base_url: http://127.0.0.1:9999/v1
    wire_api: chat
    auth: {type: api_key, env: LOCAL_KEY}
```

### Pricing

Gap G9. A provider can declare a price table in `providers.yaml`. The
[usage ledger](usage-accounting-v0.md) uses it to estimate a Turn's cost when
the host reports none, for example Codex through CPA. Prices are USD per 1M
tokens:

```yaml
providers:
  cpa:
    kind: codex-cpa
    auth: {type: api_key}
    pricing:
      input: 1.25            # uncached input
      cached_input: 0.125    # cache reads
      output: 10             # output, reasoning included
      # cache_creation_input: 1.5   # optional, defaults to input
      models:                # optional, exact model id wins over the default
        gpt-5.6-sol: {input: 2.5, cached_input: 0.25, output: 20}
```

- `input`, `cached_input` and `output` are required, whether the prices are
  the provider default or a model's.
- Values must be between 0 and 10000.
- A cost computed from this table is marked `estimated: true`.
- Claude Code reports its own `total_cost_usd`, which the ledger uses as is
  (`estimated: false`). An anthropic provider therefore needs no table.

Secret values are never stored: fields such as `api_key`, `token`, `secret`,
`password` are rejected on load.

### Agents: `<runtime-root>/agents/<id>.yaml` and `<project>/.loopx/agents/<id>.yaml`

```yaml
role: developer              # orchestrator | developer | acceptor
runtime: claude-code         # claude-code | codex-cli
provider: anthropic-login    # must exist; kind must fit the runtime
model: opus
reasoning_effort: high       # claude: low..max; codex: shared LoopX vocabulary
system_prompt_file: prompts/dev.md   # relative to this yaml; claude-code only
permission_mode: acceptEdits # claude-code (default dontAsk; acceptor: bypassPermissions)
# sandbox: read-only         # codex-cli (default read-only; acceptor: danger-full-access)
max_concurrency: 2
extra_args: ["--verbose"]    # claude: raw flags; codex: KEY=VALUE config overrides
enabled: true
```

A project file overrides the global file with the same id field by field;
`extra_args` is replaced as a whole.

**Role-based defaults (G12, design decision 35).** When the file sets no
`sandbox` / `permission_mode`, the default depends on the agent's role:

| role | codex-cli `sandbox` | claude-code `permission_mode` |
|---|---|---|
| acceptor | `danger-full-access` | `bypassPermissions` |
| developer, orchestrator | `read-only` | `dontAsk` |

The acceptor only reviews, in a throwaway detached checkout of the delivered
commit, so it runs without a sandbox to build and test freely. An explicit
value in the agent file always wins. The role is the registry role when the
caller passes it (`resolve_agent(..., role=...)`, as the dispatcher does),
otherwise the file's `role`. Python API:
`resolve_agent(agent_id, project, runtime_root=...)`,
`load_agent_definitions(...)`, `check_provider(...)`, `preflight_agent(...)`,
`turn_run_once_host_arguments(agent)`.

## CLI

- `loopx agent list|validate [--project P] [--check-auth]`, `loopx agent show <id> [--project P] [--check-auth]`
- `loopx provider list`, `loopx provider check [--name N] [--timeout-seconds S]`

`provider check` statuses: `ok`, `missing_credential`, `not_logged_in`,
`cli_missing`, `timeout`, `probe_failed`, `unsupported` (keychain off macOS).
The keychain probe runs `security find-generic-password` without `-w`.

## Turn hosts

- `loopx turn run-once --host claude-code` with `--claude-model`,
  `--claude-permission-mode` (default `dontAsk`), `--claude-effort`,
  `--claude-system-prompt-file`, `--claude-extra-arg=...`, `--claude-provider NAME`,
  `--claude-bin`. Each Turn is a fresh `claude -p --no-session-persistence`
  session; resume and subagent topology are refused.
- `--codex-config KEY=VALUE` (repeatable) and `--codex-provider NAME` add
  `-c` overrides to fresh and resumed codex-cli launches.
