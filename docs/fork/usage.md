# Using the LoopX fork: roles, the dispatcher and the CLI

This guide shows how to run a goal with the fork's role-based multi-agent
workflow and how to use the `loopx` commands involved. To see what changed
from upstream, read the [changelog](../../CHANGELOG.md). For the design
rationale, read [design-v0](design-v0.md).

- [1. How it works](#1-how-it-works)
- [2. Install](#2-install)
- [3. Quickstart](#3-quickstart)
- [4. Everyday tasks](#4-everyday-tasks)
- [5. Gates reference](#5-gates-reference)
- [6. CLI reference](#6-cli-reference)
- [7. Files and state layout](#7-files-and-state-layout)
- [8. Troubleshooting](#8-troubleshooting)
- [9. Limitations and further reading](#9-limitations-and-further-reading)

## 1. How it works

A goal has **you (the owner)** and three agent roles:

| role | what it does | what it never does |
|---|---|---|
| **orchestrator** (one per goal) | Clarifies requirements with you, proposes the plan, writes each todo's acceptance criteria, handles escalations and replans. It is the only agent that opens user gates. | It never implements todos. |
| **developer** | Implements todos in its own git worktree, commits, and delivers the work for review. | It never opens user gates or edits acceptance criteria. Blockers go to the orchestrator. |
| **acceptor** | Reviews a delivery against its criteria, in a throwaway checkout of the delivered commit. Its verdict is accept, reject or blocked. | It never modifies code. Required changes go into its rejection feedback. |

You talk to the orchestrator through **user gates**. A gate is a question or a
decision with a discussion thread. You reply to it, then close it with
approve, reject or cancel.

The **dispatcher** (`loopx dispatch serve`) runs in the background. It
decides who runs when, and launches each agent Turn with `loopx turn
run-once`. It holds no state of its own: everything is LoopX state, which the
CLI, the dashboard and the agents read and write.

The lifecycle of a goal:

```text
requirements.md
   │  loopx goal create
   ▼
orchestrator planning todo ──► clarification in a gate thread ──► plan card (plan_approval gate)
                                                                   │  you approve
                                                                   ▼
todos created (todos with dependencies start deferred)
   │
   ▼
developer Turn in worktree loopx/<goal>/<todo> ──► delivered: status in_review
                                                     │
                            acceptor Turn in a throwaway review checkout
                              ├─ reject ──► back to the developer with feedback
                              │             (the 2nd reject escalates to the orchestrator)
                              ├─ blocked ─► acceptor_blocked gate for you
                              └─ accept ──► atomic merge into the merge target
                                            (default loopx-task/<goal>) ──► dependents released
   │  all work merged
   ▼
push_request gate (you approve the push) ──► goal_complete gate (close / add work / leave open)
```

### Terms

| term | meaning |
|---|---|
| state home | The directory that holds a goal's LoopX state: the registry, the goal state file and the stored requirements doc. It can be a project directory or a separate "progress" repo. |
| registry | `<state-home>/.loopx/registry.json`. It holds the goal config: agents, roles, repos and authority sources. |
| runtime root | Durable runtime state: todos' event logs, gate threads, plan cards, workspaces, usage ledgers and the dispatcher state. The default is `~/.codex/loopx`. You can override it with the global `--runtime-root`. |
| Turn | One headless agent run (`loopx turn run-once`). It has a typed result, an independent validation and an idempotent writeback. |
| merge target | Where accepted work lands in each repo. It is the task branch `loopx-task/<goal>` with `merge_target=task_branch`, or the default branch with `merge_target=main`. |

## 2. Install

This fork is used from a source checkout.

```sh
git clone <this fork> loopx && cd loopx
uv sync --extra test                     # installs loopx plus PyYAML (needed for agent/provider files)
uv run loopx --help                      # or: .venv/bin/python -m loopx.cli --help
```

`pip install -e '.[test]'` works too. PyYAML ships only in the `test` extra. A
plain `pip install .` needs `pip install pyyaml` before `loopx agent` and
`loopx provider` can read their files.

You also need:

- `git`;
- the agent CLIs you configure: `claude` (Claude Code) and/or `codex` (Codex
  CLI). Each must be logged in, or configured with an API key; see
  [step 1](#step-1-describe-your-providers);
- optionally, the dashboard bundle. In a source checkout, build it once:
  `cd apps/presentation/dashboard && npm ci && npm run build:chat`.

The examples below write `loopx`. From a checkout, use `uv run loopx` or put
the venv on your `PATH`.

## 3. Quickstart

The example builds a feature across two repos, `api` and `web`, with three
agents:

| agent | role | runtime |
|---|---|---|
| `orch` | orchestrator | Claude Code |
| `dev` | developer | Claude Code |
| `acc` | acceptor | Codex through a CPA proxy |

The commands of steps 1 to 4 were run on a scratch state home, and so were
the gate, plan, todo, workspace and usage commands of the later steps. The
dispatcher's model Turns need your provider credentials.

### Step 1: describe your providers

A provider says how to reach a model and where its credential lives. Secrets
never go into the file: a provider names an env var, a keychain entry or a
CLI login.

`~/.codex/loopx/providers.yaml`:

```yaml
providers:
  anthropic-login:
    kind: anthropic
    auth: {type: oauth_cli}          # uses the `claude` CLI's own login
  cpa:
    kind: codex-cpa                  # defaults: base_url http://127.0.0.1:8317/v1, env CPA_API_KEY
    auth: {type: api_key, env: CPA_API_KEY}
    pricing:                         # optional: USD per 1M tokens, for cost estimates
      input: 1.25
      cached_input: 0.125
      output: 10
```

| field | values |
|---|---|
| `kind` | `anthropic`, `openai`, `openai-compatible`, `codex-cpa` |
| `auth.type` | `api_key` (with `env` and/or `keychain: {service, account}`); `oauth_cli` (the CLI's own login); `oauth_token` (for example `CLAUDE_CODE_OAUTH_TOKEN`) |
| `base_url` | Optional. For `anthropic` it becomes `ANTHROPIC_BASE_URL`. |
| `pricing` | Optional. `input`, `cached_input` and `output` are required, and `models: {<id>: {...}}` overrides them per model. Claude Code reports its own cost, so an anthropic provider needs no table. |

### Step 2: define your agents

Each agent has one file. The file name is the agent id. Global files live in
`~/.codex/loopx/agents/<id>.yaml`. A project can override them field by field
in `<project>/.loopx/agents/<id>.yaml`.

`~/.codex/loopx/agents/orch.yaml`:

```yaml
role: orchestrator
runtime: claude-code
provider: anthropic-login
model: claude-fable-5-1
reasoning_effort: medium
extra_args: ["--allowedTools=Bash,Read,Write,Edit,Glob,Grep"]
```

`~/.codex/loopx/agents/dev.yaml`:

```yaml
role: developer
runtime: claude-code
provider: anthropic-login
model: claude-opus-4-6
permission_mode: acceptEdits         # let the developer edit files
max_concurrency: 2                   # up to 2 todos of a goal in parallel
```

`~/.codex/loopx/agents/acc.yaml`:

```yaml
role: acceptor
runtime: codex-cli
provider: cpa
model: gpt-5.6-sol
# no sandbox set: the acceptor role default is danger-full-access, so it can build and test
```

| field | notes |
|---|---|
| `role` | `orchestrator`, `developer` or `acceptor`. The registry role (step 4) is the one that counts. |
| `runtime` | `claude-code` or `codex-cli`. |
| `provider` | Must exist in `providers.yaml`, and its kind must fit the runtime. |
| `model`, `reasoning_effort` | claude: `low` to `max`; codex: the LoopX effort vocabulary. |
| `system_prompt_file` | claude-code only. The path is relative to the yaml file. |
| `permission_mode` | claude-code. The default is `dontAsk`, or `bypassPermissions` for an acceptor. |
| `sandbox` | codex-cli. The default is `read-only`, or `danger-full-access` for an acceptor. |
| `max_concurrency` | How many Turns this agent runs at once. The orchestrator is always 1 per goal. |
| `extra_args` | claude: raw flags. codex: `KEY=VALUE` config overrides. A project file replaces the whole list. |
| `enabled` | `false` skips the agent. |

### Step 3: check the configuration

```sh
loopx provider list
loopx provider check                     # ok | missing_credential | not_logged_in | cli_missing | timeout | ...
loopx agent validate --check-auth        # exit 1 if any agent is invalid
loopx agent show acc                     # merged definition plus the exact `turn run-once` host flags
```

`provider check` never prints secrets. The dispatcher runs the same preflight
before it launches each agent.

### Step 4: create the goal

Write the requirements as Markdown, for example `req.md`. Its first heading
becomes the goal objective. Then:

```sh
loopx goal create --project ~/work/progress --goal-id todo-due --doc req.md \
  --repo api=~/work/api,merge_target=task_branch \
  --repo web=~/work/web,merge_target=task_branch \
  --agent orch=orchestrator --agent dev=developer --agent acc=acceptor
```

This command:

- bootstraps the goal in the state home `~/work/progress`, with the registry
  at `~/work/progress/.loopx/registry.json`;
- copies `req.md` to `docs/goals/todo-due/req.md` and registers it as the
  goal's requirements source;
- registers the three agents with their roles, sets `agent_model=role_v1`,
  and declares the repos;
- adds the orchestrator's first todo: clarify the requirements, then propose
  a plan.

Notes:

- The `--repo` spec is `NAME=PATH[,default_branch=B][,merge_target=main|task_branch][,task_branch=B]`.
  A relative path resolves against the current directory.
- Exactly one `--agent ID=orchestrator` is required.
- `--dry-run` previews the result. `--no-global-sync` keeps the goal out of
  the shared registry.
- Running the command again is safe, because every step is an upsert.

**Where to run later commands.** In the state home directory, `loopx` finds
the project registry by itself. Elsewhere, pass
`--registry ~/work/progress/.loopx/registry.json`.

### Step 5: start the dispatcher

```sh
cd ~/work/progress
loopx dispatch serve --goal-id todo-due --project . --max-global 3
```

- `serve` watches the state files and runs a reconcile pass every
  `--tick-seconds` (default 60). It prints one JSON line per pass that acts,
  and an idle heartbeat line every 15 minutes.
- `--once` runs one pass, waits for the Turns it launched, and exits. Use it
  for cron or for debugging.
- Only one dispatcher runs per runtime root. A second one exits with code 3.
- To keep it running on macOS, generate a launchd job. The command only
  prints it:

```sh
loopx dispatch launchd-plist --goal-id todo-due --project . > ~/Library/LaunchAgents/com.loopx.dispatch.plist
launchctl load ~/Library/LaunchAgents/com.loopx.dispatch.plist
```

Once running, the dispatcher launches the orchestrator on its planning todo.

### Step 6: clarify and approve the plan

The orchestrator asks its questions in a gate thread. It usually proposes a
plan card at the same time, as a `plan_approval` gate.

```sh
loopx gate list --goal-id todo-due                         # open gates, who each awaits
loopx gate show --goal-id todo-due --todo-id <gate-id>     # the thread, and for a plan the todo table
loopx plan show --goal-id todo-due --plan-id <plan-id>     # the full plan card
loopx gate reply --goal-id todo-due --todo-id <gate-id> --text "Use the local date; do the contract first."
```

Your reply wakes the orchestrator. It answers in the thread, and it can revise
the plan in place (`plan propose --revise`). When the plan is right, approve
it:

```sh
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --decision approve
```

LoopX then creates the plan's todos:

- todos with dependencies start `deferred`;
- each todo carries its acceptance criteria and validation command;
- the orchestrator's planning todo is closed automatically.

A `reject` or `cancel` changes nothing.

### Step 7: watch progress

```sh
loopx todo list --goal-id todo-due          # todos plus "Dependency waits"
loopx dispatch status                       # running Turns, per-agent slots, cooldowns, dispatcher gates
loopx status --goal-id todo-due             # goals, gates, attention queue
loopx usage report --goal todo-due --by role
loopx dashboard --goal-id todo-due          # browser: Role board tab (角色看板)
```

The dashboard's **Role board** shows:

- "Waiting on you" at the top;
- the columns Planned, Assigned, Rework, Running, In review and Done, in
  orchestrator, developer and acceptor swimlanes;
- a cost strip.

Clicking a gate opens its drawer, where you can read the thread, reply and
decide.

What happens on its own:

- A developer works in `<runtime-root>/goals/todo-due/workspaces/<todo>/<repo>`
  on the branch `loopx/todo-due/<todo>`. It commits, and it never pushes.
- When the developer's Turn succeeds, LoopX runs the todo's validation
  command and moves the todo to `in_review`.
- The acceptor reviews a detached checkout of the delivered commit.
  - **Reject** reopens the todo for the developer with feedback that names
    the failed criteria. The second reject blocks the todo and hands it to
    the orchestrator.
  - **Accept** merges the delivered commit into the merge target of every
    repo, atomically, then marks the todo done. This releases its dependents.

### Step 8: answer gates as they come

The orchestrator opens a gate when it needs you. LoopX opens system gates for
blocked reviews, budgets, re-logins, pushes and completion. Section
[5](#5-gates-reference) lists every kind and its options. The common ones:

```sh
# The acceptor could not review (for example missing tooling). Pick one option:
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option retry_acceptance
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option accept_manually --note "checked by hand"
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option return_to_developer --note "install node 22 first"
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option cancel_todo
```

### Step 9: push

When no agent todo of the goal is open, in review or blocked, and a merge
target has commits that its remote lacks, the dispatcher opens a
`push_request` gate. The gate lists each repo's branch, its remote and the
commit range.

```sh
loopx gate show --goal-id todo-due --todo-id <gate-id>
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --decision approve   # git push <remote> <branch>, never force
```

`reject` or `cancel` pushes nothing, and the same heads are not offered again.
A repo without a remote is skipped. To push without a gate, run `git push`
yourself.

### Step 10: close the goal

Once the work is merged and the push is resolved, the dispatcher opens one
`goal_complete` gate. It needs no model Turn, and it shows the merges, the
push results, the review counts and the cost.

```sh
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option close_goal     # stop the goal (reversible)
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option add_work --note "Also sort by due date"
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option leave_open
```

`add_work` turns your note into an orchestrator todo, so planning continues.

## 4. Everyday tasks

### Talk to the orchestrator

- **Answer or ask in an open gate:** `loopx gate reply --goal-id G --todo-id
  <gate> --text "..."`.
- **Ask for new work mid-flight:** add a todo for the orchestrator. It plans
  the work, and a major change comes back to you as a plan card.

  ```sh
  loopx todo add --goal-id G --role agent --text "Also support sorting by due date" \
    --required-role orchestrator --requires-acceptance false
  ```
- **After the goal is done:** use the `goal_complete` gate's `add_work`
  option.

### Change a todo's acceptance criteria

After planning, criteria change only through a plan card that you approve.
Ask the orchestrator in a gate or todo. It proposes a card with
`criteria_changes`, and you see the old and new criteria side by side in
`loopx plan show` or `gate show`. While the card is pending, the acceptor
does not review that todo.

As the owner, you can still edit criteria directly. Pass no `--agent-id`. The
edit is logged as a major change.

```sh
loopx todo update --goal-id G --todo-id T --acceptance-criteria "GET /todos?overdue=true returns only overdue items"
```

An agent that tries this, the orchestrator included, is refused with
`acceptance_criteria_change_requires_plan`.

### Replace or split a todo

```sh
loopx todo add --goal-id G --role agent --text "Web: render due" --task-repo web --acceptance-criteria "..."
loopx todo supersede --goal-id G --todo-id OLD --by NEW1,NEW2 --agent-id orch --note "split"
```

The command closes `OLD` as superseded, and every todo that depended on it
now depends on all the replacements. A superseded todo never counts as done.
Only the orchestrator, or the owner with no `--agent-id`, may do this. Normally
the orchestrator does it after an escalation.

### Budgets and cost

```sh
loopx usage budget --goal G --set 50          # USD; --clear removes it; no flag shows spend against budget
loopx usage report --goal G --by todo         # also --by role|agent|goal|model|day, --days N, --since ISO, --format json
```

- With no budget set, nothing happens: no alert, no gate, no pause.
- At 80% of the budget, LoopX opens a non-blocking alert.
- At 100%, a `budget_exhausted` gate stops new Turns of the goal. Running
  Turns finish. Resolve the gate with:
  - `raise_budget --note 75`, where the note gives the new amount. Without an
    amount, the budget grows by 50%.
  - `continue_without_limit`.
  - `stop_goal`.

Codex through CPA reports no USD. Its cost comes from the provider's
`pricing` table and is marked estimated.

### Stop and resume a goal

```sh
loopx goal-lifecycle --goal-id G --operation stop --actor-kind owner --execute
loopx goal-lifecycle --goal-id G --operation resume --actor-kind owner --execute
```

Stopping a goal keeps its todos and history, and the dispatcher skips the
goal. Without `--execute`, the command only previews. The `close_goal` and
`stop_goal` gate options use the same stop.

### Run or debug a single step

```sh
loopx dispatch serve --goal-id G --once                     # one pass; waits for the Turns it launched
loopx quota should-run --goal-id G --agent-id dev           # would this agent run now, and on which todo?
loopx agent show dev                                        # the host flags the dispatcher passes
loopx workspace status --goal-id G --todo-id T              # dirty, ahead or behind, predicted conflicts
```

The dispatcher stores each Turn's output in
`<runtime-root>/dispatch/runs/<run>.*`.

### Act on todos by hand

The dispatcher does these steps for you. You rarely need them, but they are
handy for recovery and scripting.

```sh
loopx workspace prepare --goal-id G --todo-id T             # worktree per repo on loopx/G/T
loopx workspace merge   --goal-id G --todo-id T --dry-run   # atomic, all repos or none; never pushes
loopx workspace cleanup --goal-id G --todo-id T             # only when merged (or --force)
loopx todo accept       --goal-id G --todo-id T --agent-id acc [--note ...]
loopx todo reject       --goal-id G --todo-id T --agent-id acc --note "criterion 2 fails: ..."
loopx todo block-review --goal-id G --todo-id T --agent-id acc --reason "toolchain missing"
loopx goal request-push --goal-id G                         # open the push gate now
```

## 5. Gates reference

Every gate has a thread (`gate reply`, `gate show`) and closes with
`loopx gate resolve --goal-id G --todo-id GATE ...`. You can also close it
from the dashboard drawer, which lists the same options.
`loopx todo complete --role user --decision-outcome ...` is the older
equivalent.

| kind | opened by | holds | resolve with |
|---|---|---|---|
| `decision` | the orchestrator: a question or a choice | the agent it blocks | `--decision approve \| reject \| cancel`, after replying in the thread |
| `plan_approval` | `loopx plan propose` | the orchestrator | `approve` applies the plan. `reject` and `cancel` apply nothing. Reply in the thread to request changes. |
| `acceptor_blocked` | LoopX, when the acceptor gives a blocked verdict | the acceptor, on that todo | `--option retry_acceptance \| accept_manually` (approve), `return_to_developer` (reject), `cancel_todo` (cancel) |
| `push_request` | the dispatcher once all work is merged, or `loopx goal request-push` | the orchestrator | `approve` pushes each merge target. `reject` and `cancel` decline. |
| `budget_exhausted` | the dispatcher at 100% of a goal budget | all new Turns of the goal | `--option raise_budget --note <USD> \| continue_without_limit` (approve), `stop_goal` (reject) |
| `goal_complete` | the dispatcher once the goal is finished | the orchestrator | `--option close_goal` (approve), `add_work --note "..."` (reject), `leave_open` (cancel) |
| re-login needed | the dispatcher when an auth preflight fails | that agent | Log in again (`claude auth` or `codex login`), then approve. |
| provider in long cooldown | the dispatcher when a provider backoff reaches `--long-cooldown-seconds` | the agent that hit it | Close it when the provider is usable again. |
| orchestrator repeat limit | the dispatcher when orchestrator action todos keep failing | the orchestrator | Check the goal, then close it. |

A bare `--decision` selects the option in its row: approve selects the first
option, reject the reject option, and cancel the cancel option.

## 6. CLI reference

Global options go before the command:

| option | meaning |
|---|---|
| `--registry PATH` | The registry to use. Inside a state home, the project registry is found by itself. |
| `--runtime-root PATH` | The runtime state root. The default is the registry's `common_runtime_root`, else `~/.codex/loopx`. |
| `--format markdown\|json` | The output format. Most subcommands also accept `--format` after the subcommand. |

Every command has `--help`. `loopx commands` lists all commands, including
the upstream ones not covered here.

### `loopx goal`

| command | purpose |
|---|---|
| `goal create --project DIR --goal-id G --doc FILE [--repo SPEC]... --agent ID=ROLE... [--objective TEXT] [--no-global-sync] [--dry-run]` | Create a role_v1 goal from a requirements doc (step 4). |
| `goal request-push --goal-id G [--agent-id ORCH] [--dry-run]` | Open the `push_request` gate now. It does not wait for pending todos. |

### `loopx agent`, `loopx provider`

| command | purpose |
|---|---|
| `agent list [--project P]` | Merged agent definitions: global plus the project's `.loopx/agents`. |
| `agent show ID [--project P] [--check-auth]` | One agent, its sources and its `turn run-once` host flags. |
| `agent validate [ID] [--project P] [--check-auth]` | Validate agent files. Exits 1 on an invalid agent. |
| `provider list` | The configured providers, without secrets. |
| `provider check [--name N] [--timeout-seconds S]` | The auth preflight. |

### `loopx dispatch`

| command | purpose |
|---|---|
| `dispatch serve --goal-id G [--goal-id G2 ...] [options]` | Run the resident dispatcher. |
| `dispatch status` | Running Turns, per-agent slots, provider and agent cooldowns, gates the dispatcher opened, retry identities. |
| `dispatch launchd-plist --goal-id G [options] [--label L]` | Print a launchd job that runs `serve`. It installs nothing. |

`serve` options (`launchd-plist` takes the same):

| option | default | meaning |
|---|---|---|
| `--project P` | the goal's legacy `repo`, else the registry's directory | Where project agent files live, and the default Turn cwd. |
| `--tick-seconds` | 60 | The periodic reconcile interval. |
| `--poll-seconds` | 3 | The state-file watch interval. |
| `--max-global` | 4 | A machine-wide cap on concurrent Turns. |
| `--turn-timeout-seconds` | 3600 | The per-Turn timeout, passed to `turn run-once` as `--timeout-seconds`. |
| `--long-cooldown-seconds` | 3600 | A provider cooldown this long opens a user gate. |
| `--backoff-base-seconds` | 60 | The first provider backoff step, doubling up to 6 h. |
| `--idle-heartbeat-seconds` | 900 | Prints an idle line after this long without output. `0` disables it. |
| `--validation-command-json '["make","test"]'` | none | The fallback validator for todos that declare none. Without it, those Turns fail validation. |
| `--no-global-sync` | off | Passes `--no-global-sync` to every Turn. |
| `--once` | off | Runs one pass, waits for its Turns, and exits. |

### `loopx gate`

| command | purpose |
|---|---|
| `gate list --goal-id G [--awaiting user\|orchestrator]` | Open user gates, their kind and who they await. |
| `gate show --goal-id G --todo-id GATE` | Status, kind, thread and kind-specific content: the plan, push repos, budget or completion summary. |
| `gate reply --goal-id G --todo-id GATE --text TEXT [--as user\|orchestrator --agent-id ORCH]` | Append to the thread. The default is you. Replying never closes a gate. |
| `gate resolve --goal-id G --todo-id GATE [--decision approve\|reject\|cancel] [--option OPT] [--note TEXT] [--agent-id A] [--dry-run]` | Close a gate. For options, see [section 5](#5-gates-reference). |

### `loopx plan`

The orchestrator uses these commands. You mostly read them.

| command | purpose |
|---|---|
| `plan propose --goal-id G --agent-id ORCH --plan-file plan.json [--revise PLAN_ID]` | Propose a plan card, or revise a pending one in place. The card opens a `plan_approval` gate. |
| `plan show --goal-id G --plan-id P` | The plan's todos, dependencies, criteria and criteria changes. |
| `plan list --goal-id G [--require-status pending\|applying\|applied\|rejected\|cancelled]` | The goal's plans. With `--require-status`, exits 1 unless a plan has that status. |
| `plan apply --goal-id G --plan-id P` | Recovery only. Finishes an interrupted apply of an approved plan. |

A plan file looks like this:

```json
{
  "title": "Todo due dates v1",
  "summary": "Contract first, then API and web in parallel, then integration.",
  "todos": [
    {"key": "contract", "text": "Write the API contract", "task_repositories": ["api"],
     "acceptance": "openapi.yaml documents the optional ISO due field",
     "validation_command": "test -f openapi.yaml", "priority": "P1"},
    {"key": "api", "text": "Implement due in the API", "depends_on": ["contract"], "task_repositories": ["api"]},
    {"key": "web", "text": "Show due in the web client", "depends_on": ["contract"], "task_repositories": ["web"]},
    {"key": "integrate", "text": "Integrate web and API", "depends_on": ["api", "web"], "task_repositories": ["api", "web"]}
  ]
}
```

Each todo item accepts these fields:

- `key` and `text`;
- `priority` (`P0` to `P3`);
- `required_role` (default `developer`);
- `bound_agent` and `acceptor_agent`;
- `depends_on`, or its inverse `successors`;
- `requires_acceptance`;
- `task_repositories`;
- `acceptance` (at most 1000 characters);
- `validation_command`, `estimated_effort` and `action_kind`.

List the todos in dependency order. A plan can also carry
`criteria_changes: [{todo_id, new, reason, old?}]` for existing todos. For
the full format, see [gates-plans-intake-v0](gates-plans-intake-v0.md#plan-cards-decision-12).

### `loopx todo` (role_v1 additions)

These are the fork's additions to the upstream `todo` command:

| command or flag | purpose |
|---|---|
| `todo accept --goal-id G --todo-id T --agent-id ACC [--note] [--evidence]` | Accept an `in_review` todo. It merges first, then completes. |
| `todo reject --goal-id G --todo-id T --agent-id ACC --note TEXT` | Reject it. The note is required and should name the failed criteria. The second reject escalates. |
| `todo block-review --goal-id G --todo-id T --agent-id ACC --reason TEXT` | The acceptor cannot review. This opens an `acceptor_blocked` gate. |
| `todo supersede --goal-id G --todo-id OLD --by NEW[,NEW2] [--agent-id ORCH] [--note]` | Replace or split a todo, and rewire its dependents. |
| `todo complete ...` | On a role_v1 goal with an acceptor, delivers the todo to `in_review` instead of marking it done. |
| `--status in_review` | A first-class status. `todo add` cannot create it. |
| `--required-role orchestrator\|developer\|acceptor` | Route a todo to a role. The default is developer for work, and orchestrator for gates, blockers and replans. |
| `--requires-acceptance true\|false` | Whether completion needs an acceptor. The default is true for developer work. |
| `--acceptor-agent ID` | Bind a specific acceptor. |
| `--task-repo NAME` (repeatable) | The goal repos the todo touches. Several repos make an atomic multi-repo todo. |
| `--acceptance-criteria TEXT` | The todo's criteria. Only the orchestrator, or you, may set them. |
| `--review-feedback TEXT` | Rework instructions for the developer (`todo update`). |
| `--reject-count N` | The rejection counter. |
| `--clear-*` | Clears the matching field: `--clear-required-role`, `--clear-acceptor-agent`, `--clear-task-repos`, `--clear-acceptance-criteria`, `--clear-review-feedback`. |

`loopx todo list` shows `dependency_waits`: why each deferred todo is still
waiting.

### `loopx workspace`

`loopx workspace prepare | status | merge | cleanup --goal-id G --todo-id T
[--repo NAME]... [--dry-run]`. `cleanup` also takes `--force`. Without
`--repo`, a command uses the todo's `task_repositories`, else every goal repo.

| action | effect |
|---|---|
| `prepare` | Creates or reuses one worktree per repo at `<runtime-root>/goals/G/workspaces/T/<repo>`, on the branch `loopx/G/T`, starting from the merge target. |
| `status` | Per repo: whether the worktree is dirty, how far it is ahead of or behind the target, unmerged paths and predicted conflicts. |
| `merge` | A no-ff merge into every repo's target with `LoopX-Goal` and `LoopX-Todo` trailers. It merges all repos or none, and never pushes. |
| `cleanup` | Removes this todo's worktrees and branch once merged, or with `--force`, which discards the work. |

### `loopx usage`

| command | purpose |
|---|---|
| `usage report [--goal G]... [--since ISO \| --days N] [--by role\|agent\|goal\|todo\|model\|day] [--format json\|text\|markdown]` | Turns, agent-hours, tokens and cost, which may be reported or estimated. It also shows the cost per accepted todo with and without the orchestrator. |
| `usage budget --goal G [--set USD \| --clear]` | Show, set or clear a goal budget. |

### Goal configuration: `configure-goal` and `register-agent`

`goal create` covers the usual setup. Use these commands to change a goal
later. `configure-goal` only previews until you add `--execute`.

```sh
loopx configure-goal --goal-id G --agent-role dev2=developer --execute     # assign a role (repeatable)
loopx configure-goal --goal-id G --clear-agent-role dev2 --execute
loopx configure-goal --goal-id G --agent-model role_v1 --execute           # move a peer_v1 goal to roles
loopx configure-goal --goal-id G --repo docs=/abs/docs,default_branch=main,merge_target=main --execute
loopx configure-goal --goal-id G --clear-repos --execute
loopx register-agent --goal-id G --agent-id dev2 --role developer --execute
```

Two rules apply:

- A goal has at most one orchestrator. To move the role, clear the old
  orchestrator and assign the new one in the same call.
- A role must name a registered agent.

### `loopx turn run-once` (host flags)

The dispatcher builds these flags from the agent file (see `loopx agent show
ID`). You only need them to run a Turn by hand.

| flag | meaning |
|---|---|
| `--host claude-code \| codex-cli` | The built-in host. |
| `--claude-model`, `--claude-effort low..max`, `--claude-permission-mode`, `--claude-system-prompt-file`, `--claude-extra-arg=--flag`, `--claude-provider NAME`, `--claude-bin` | Claude Code. Each Turn is a fresh `claude -p` session. |
| `--codex-config KEY=VALUE` (repeatable), `--codex-provider NAME` | Codex config overrides, passed as `-c`. The provider expands its CPA or openai-compatible definition. |
| `--todo-id T` | Pin the Turn to one eligible todo. |

Required: `--goal-id`, `--agent-id` and `--project`. Add `--execute` to run
for real.

### Upstream commands you will still use

| command | purpose |
|---|---|
| `status [--goal-id G]` | Goals, gates, the attention queue and the next action. `--format json` includes `role_board` and `turn_usage_summary`. |
| `dashboard [--goal-id G] [--port 8767] [--no-open]` | The local web UI: role board, gate drawer and discussion panel. |
| `goal-lifecycle --goal-id G --operation stop\|resume --actor-kind owner --execute` | A reversible stop or resume. |
| `quota should-run --goal-id G --agent-id A` | Whether an agent runs now, and on which todo. |
| `todo list --goal-id G` | Todos, with dependency waits. |

## 7. Files and state layout

In the state home:

```text
<state-home>/
  .loopx/registry.json                   goal config: agents, roles, repos, authority sources
  .loopx/agents/<id>.yaml                optional project overrides of agent files
  .codex/goals/<goal>/ACTIVE_GOAL_STATE.md   the goal state file (todos)
  docs/goals/<goal>/<requirements>.md    the stored requirements doc
```

In the runtime root (default `~/.codex/loopx`):

```text
<runtime-root>/
  providers.yaml, agents/<id>.yaml       provider and agent definitions
  goals/<goal>/
    rollout-event-log.jsonl              goal events (plan, gate, review, push, budget, ...)
    gates/index.json, gates/<gate>.jsonl gate index and discussion threads
    plans/<plan>.json                    plan cards
    plans/supersessions.jsonl            todo supersessions (dependency rewiring)
    workspaces/<todo>/<repo>             developer worktrees
    reviews/<todo>/<attempt>/<repo>      throwaway acceptor checkouts (removed after each review)
    push/state.json                      declined heads and last push results
    usage.jsonl, usage-budget.json       usage ledger and optional budget
  dispatch/
    serve.lock, state.json               single-instance lock and dispatcher state
    runs/<run>.*                         each Turn's output
    logs/                                launchd output
```

In each code repo:

- `loopx/<goal>/<todo>`: one todo branch.
- `loopx-task/<goal>`: the default merge target when `merge_target=task_branch`.

## 8. Troubleshooting

| symptom | cause and fix |
|---|---|
| `dispatch serve` exits with code 3 (`dispatcher_locked`) | Another dispatcher already serves this runtime root. Check it with `loopx dispatch status`. |
| An agent never runs, and `dispatch status` shows it unavailable | Its auth preflight failed. Run `loopx provider check`, log in again, then approve the "needs re-login" gate. |
| A provider is in cooldown | It hit a rate limit, a quota or an upstream 5xx. The provider backs off automatically, and a long cooldown opens a gate. |
| A todo is never picked up | Run `loopx todo list` and read "Dependency waits": a dependency that requires acceptance must be accepted and merged. Then check `loopx quota should-run --goal-id G --agent-id A`. |
| Every Turn of a todo fails validation | The todo declares no validation command, and `serve` has no `--validation-command-json`. Add either one. |
| `workspace_unverified` | The todo's worktree is missing or off its branch. Run `loopx workspace prepare` again. It reuses the existing branch. |
| `acceptance_criteria_change_requires_plan` | Criteria changes need a plan card. Ask the orchestrator, or edit as the owner without `--agent-id`. |
| `not_orchestrator` on `todo supersede --by` | Only the orchestrator (`--agent-id ORCH`), or the owner with no `--agent-id`, may supersede. |
| A multi-agent lifecycle command asks for `--agent-id` | Owner writes on a claimed todo are attributed to its claim owner. For a plain `todo claim` or `supersede`, pass the acting agent with `--agent-id`. |
| `accept_manually` reports `agent_id='owner' is not registered` | The delivered todo had no claim owner. The merge landed and the gate closed, but the todo is still `in_review`. Finish it as the acceptor: `loopx todo accept --goal-id G --todo-id T --agent-id ACC --note "accepted manually"`. |
| The orchestrator seems to ignore the goal state | The state digest and system-prompt addendum only reach claude-code agents. Run the orchestrator on claude-code. |
| The dashboard shows no chat or role board in a source checkout | Build the bundle: `cd apps/presentation/dashboard && npm run build:chat`. |

## 9. Limitations and further reading

The [changelog](../../CHANGELOG.md#known-limitations) lists the current
limitations. The main ones:

- No `in_review` support on canonical `hard_lease` goals.
- No remote-SSH workspaces or resource pool yet.
- The orchestrator digest is claude-code only.

Deeper references:

- [role-v1-protocol](role-v1-protocol.md): roles, todo fields, selection,
  verdicts, criteria, supersession.
- [dispatcher-v0](dispatcher-v0.md): what one pass does, cooldowns, crash
  recovery, the orchestrator digest.
- [gates-plans-intake-v0](gates-plans-intake-v0.md): gate threads, plan
  cards, goal intake, the goal-complete gate.
- [workspaces-v0](workspaces-v0.md): repos, worktrees, atomic merge,
  delivery identity, push.
- [agent-and-provider-config](agent-and-provider-config.md): agent and
  provider files, Turn hosts.
- [usage-accounting-v0](usage-accounting-v0.md): the usage ledger, reports,
  pricing, budgets.
- [role-board-v0](role-board-v0.md): the status projection and the dashboard
  tab.
- [e2e-pilot-report-v1](e2e-pilot-report-v1.md): a full run on real models.
