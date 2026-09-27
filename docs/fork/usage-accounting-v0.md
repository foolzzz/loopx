# Usage accounting v0 (gap G9)

Resolves gap G9 of the [E2E pilot report](e2e-pilot-report-v0.md): the
claude-code and codex-cli Turn hosts dropped `total_cost_usd`, `num_turns` and
token usage. Every Turn now records what it cost, so you can report
agent-hours, cost and Turns per role, agent, goal, todo, model and day.

## What a Turn records

Each built-in host returns a typed `turn_usage` block (`loopx_turn_usage_v0`)
next to its typed result. The agent's result contract does not change. Code:
`loopx/control_plane/turn_driver/turn_usage.py`.

| host | source | fields |
|---|---|---|
| claude-code | the `claude -p --output-format json` result object | `total_cost_usd` (as `cost_usd`, not estimated), `num_turns`, `duration_ms` and `duration_api_ms` (as `host_duration_ms` and `api_duration_ms`), `usage` tokens (input, cache read, cache creation, output, thinking), and one entry per model from `modelUsage` with its own tokens and cost |
| codex-cli | the `turn.completed` event of `codex exec --json` (or the older `token_count` event) | input (minus cached), cached input, cache write, output, reasoning output. There is no USD. The model is `--codex-model`, or a `model=` config override |
| both | the adapter | wall-clock `duration_ms`, `started_at`, `finished_at` |

The block only holds counts, money, durations, model ids and a sha256 digest
of the codex thread id. It never holds prompt or result text, session ids or
credentials.

Failed Turns keep their usage, because the money was still spent:
- A host error (`BuiltInHostError`) carries `turn_usage`.
- A timeout, or a crash with no output, records the wall-clock duration only
  (`source: wall_clock_only`).

The executor journals the block per host attempt (`host_attempt`). The
`turn run-once` payload shows it as `turn_usage`, and shows the ledger write as
`usage_ledger`.

**Codex session totals.** Codex reports totals for the whole session. After a
resume, `turn.completed` also counts the earlier Turns. We checked this against
codex-cli 0.153:
- The block marks such totals `cumulative`, with the session digest.
- The ledger subtracts the last recorded Turn of the same session.
- If a resumed session's previous Turn was never recorded, the row is flagged
  `cumulative_unresolved`, because its numbers over-count.

## The ledger

`<runtime_root>/goals/<goal>/usage.jsonl` is append-only. It has one row
(`loopx_usage_ledger_entry_v0`) per host attempt of a Turn. Code:
`loopx/usage_accounting/ledger.py`.

- **Key.** `entry_id = <turn_key>#<host_attempt>`.
  - A replayed or resumed settled Turn reads the same journaled usage, so the
    write is a no-op.
  - A retry that called the host again is a new attempt and is counted.
- **Writes** happen under a file lock after `run_loopx_turn_once` returns
  (`loopx/cli_commands/turn_usage_record.py`). A write never fails the Turn;
  an error only shows up in `usage_ledger.reason`.
- **Row fields:**
  - ids: turn key, attempt, goal, agent, todo;
  - `role` (the registry role);
  - `provider`: `--claude-provider` or `--codex-provider`, else the agent
    file's provider;
  - `model` and per-model entries;
  - `host`, `outcome` (result kind), `status`, `failure_kind`;
  - timestamps, `duration_ms`, `num_turns`, `tokens`, `cost_usd`;
  - `cost_source` (`host_reported`, `estimated` or `unpriced`) and
    `cost_estimated`.

**Why not the existing seam.** The kernel's quota spend counts scheduler slots,
always one per Turn, and has no tokens or money. The upstream `run_usage_v0`
seam attaches usage to run-history records. That seam does not fit, for three
reasons:
- Run records exist only for committed Turns.
- It has no role, todo or estimated dimensions.
- It fails closed inside the writeback.

So G9 has its own ledger. The status `usage_summary` (the run-history proxy) is
unchanged.

## Estimated pricing

Codex through CPA reports no USD. To get an estimate, add a `pricing` section
to the provider in `providers.yaml` (see
[agent-and-provider-config](agent-and-provider-config.md#pricing)). The
ledger then computes the cost as follows:

```
(input * input + cached_input * cached_input
 + cache_creation_input * (cache_creation_input or input)
 + output * output) / 1e6
```

It uses the model's rates, or else the provider default, and marks the cost
`estimated`. Anthropic's reported cost is always used as is (`estimated:
false`). A Turn with no reported cost and no price stays `unpriced` and is
counted separately.

## Report

```sh
loopx usage report [--goal G ...] [--since ISO | --days N] \
  [--by role|agent|goal|todo|model|day] [--format json|text]
```

The report covers these totals, and the same fields per group:

| field | meaning |
|---|---|
| `turns`, `failed_turns` | Turns in the window, and how many of them failed |
| `model_turns` | the host's own turn count |
| `agent_hours` | the sum of Turn wall-clock durations, so parallel agents add up |
| `tokens` | tokens by type |
| `cost_usd` | total cost; `cost_reported_usd` and `cost_estimated_usd` split it |
| `unpriced_turns` | Turns with neither a reported cost nor a price |
| `todos` | todos with spend in the window |
| `accepted_todos` | those of them that are done with an accept record: completion evidence `accepted_by=<acceptor>`, which includes the owner's manual accept (`accepted_by=owner`) |
| `cost_per_accepted_todo_usd`, `turns_per_accepted_todo` | cost and Turns per accepted todo |
| `orchestrator_turns`, `orchestrator_cost_usd` | the part of the Turns and cost spent by `role=orchestrator` agents |
| `cost_per_accepted_todo_excl_orchestrator_usd` | cost per accepted todo without the orchestrator's spend |

A todo that is merely `done` is not accepted: the orchestrator's planning
todo, a dispatcher action todo it retired, a superseded todo, or a todo
completed without acceptance (pilot v1 gap N5). A goal without acceptors
(`peer_v1`) therefore reports 0 accepted todos and no per-accepted-todo cost.
The text report shows the orchestrator's share and the figure without it on
its `total` line.

Other rules:
- Without `--goal`, the report covers every registry goal (`scope:
  registry`). `--by day` then gives the per-person, per-day view.
- `--by model` splits a multi-model claude Turn by its per-model cost.
- Days are local dates.

## Status and dashboard

`run_history.goals[].turn_usage_summary` (`loopx_turn_usage_summary_v0`)
contains:
- turns, failed Turns, agent-hours, total tokens;
- cost, with the estimated part and the unpriced Turns;
- accepted todos (accept records only), cost and Turns per accepted todo,
  and the cost per accepted todo without the orchestrator;
- the orchestrator's cost and Turns;
- the per-role split (at most 8 rows);
- the budget share, when a budget is set.

It is attached to every goal that has a ledger, peer_v1 goals included. Its
reads are bounded:
- it aggregates only the latest 5000 rows (`truncated: true` when rows were
  cut);
- accepted todos come from the status `todo_index` (its completion
  evidence), which is capped;
- a missing or broken ledger just leaves the summary out.

Code: `loopx/control_plane/status/usage_projection.py`.

The dashboard's role board shows it as one strip under its header (see
[role-board-v0](role-board-v0.md#usage-strip)).

## Budget (optional)

Budgets are optional and usually unset (design decision 41). Without a
budget, nothing below happens: no alert, no gate, no pause.

```sh
loopx usage budget --goal G --set 50    # USD; --clear removes it; no flag shows spend vs budget
```

The budget lives in `<runtime_root>/goals/<goal>/usage-budget.json`. It is not
in the registry, so no goal-config schema changes.

On every pass, the dispatcher compares the ledger's total cost with the budget
(`loopx/dispatch/usage_budget.py`).

**80%: a non-blocking alert.**
- Once per budget value, it opens one user todo when spend crosses 80%.
- The todo is a `user_action`, not a `user_gate`, so it never pauses work.
- The pass reports it as `gates_opened` with key `usage_budget:80`.
- The dispatcher state (`budget_alerts`) remembers which alerts it opened. It
  also adopts an open alert by its text, so a lost state file does not
  duplicate it.

**100%: the `budget_exhausted` gate.** Code: `loopx/usage_budget_gate.py`.
- The dispatcher opens one system user gate of kind `budget_exhausted`
  (report key `usage_budget:100`) instead of a second alert. If spend jumps
  past 80% and 100% at once, the gate covers both.
- The gate text shows the spend against the budget, the estimated part of the
  spend and its share, unpriced Turns, the per-role split and the default
  raise. `loopx gate show` also lists them (`budget_usd`, `spent_usd`,
  `estimated_usd`, `by_role`, `default_raise_usd`).
- While the gate is open, the dispatcher launches no new Turn of that goal
  for any role (skip reason `budget_exhausted_gate_open`). Turns already
  running finish normally and are never killed. Other goals are unaffected.
- On role_v1 goals the gate blocks the orchestrator's lane (like the push
  gate); otherwise it is goal-bound.
- One gate per goal and crossing. A crossing is one budget revision: the
  value and `updated_at` of `usage-budget.json`. The gate index remembers the
  revision, so a replayed tick, a restarted dispatcher or a lost state file
  never opens a second gate, and more spend on the same budget does not either.
- If the budget is cleared or raised above the spend with `loopx usage
  budget` while the gate is open, the hold ends; close the stale gate with
  any option.

Resolve the gate with `loopx gate resolve --goal-id G --todo-id <gate>
--option …` or the dashboard's `gate.resolve` (the drawer lists the three
options; the decision note carries the amount):

| option | decision | effect |
|---|---|---|
| `raise_budget` | approve | Sets the budget to the first number in the note (`--note 75`, `$75`, `raise to 1,200`), or +50% of the current budget without one. Dispatching resumes, and the 80% alert and 100% gate re-arm for the new value. A raise that does not cover the spend is refused and the gate stays open. |
| `continue_without_limit` | approve | Clears the budget. Dispatching resumes. |
| `stop_goal` | reject (or cancel) | Records the owner's decision and stops the goal with the existing reversible goal stop (`loopx goal-lifecycle`). Todos are kept. The budget stays. |

`--decision approve` alone means `raise_budget`; `reject` or `cancel` alone
means `stop_goal`. The outcome is recorded on the gate (`budget_outcome`,
`decision_option`) and as `usage_budget_exhausted` / `usage_budget_decided`
rollout events.

**Resuming a stopped goal.** The dispatcher holds a goal stopped at its
budget gate (skip reason `budget_stopped_by_owner`) until the owner resumes
it:

```sh
loopx goal-lifecycle --goal-id G --operation resume --actor-kind owner --execute
```

The resumed goal runs on. Its crossing is already decided, so no new gate
opens until the budget changes. To keep a limit, set a higher budget with
`loopx usage budget --goal G --set USD` before or after resuming.

## Known gaps

- Estimates are only as good as the price table, and prices drift.
- Codex usage after a timeout is lost when the kill came before
  `turn.completed`; only the duration is recorded.
- A crash of `turn run-once` before the ledger write loses nothing, since the
  journal holds the usage. It is written on the next replay of that Turn. A
  Turn that is never replayed stays unrecorded.
- The ledger keeps the first outcome of an attempt. A later replay that
  settles a previously failed settlement does not rewrite it. Accepted todos
  come from current todo state, so the per-todo numbers stay right.
- Generic-cli and dsh hosts record no usage.
- The budget pause acts on the spend already in the ledger. Turns running
  when the gate opens still finish and add their cost, so the spend can end
  above the budget.
