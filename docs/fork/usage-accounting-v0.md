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
| `accepted_todos` | those of them that are done now |
| `cost_per_accepted_todo_usd`, `turns_per_accepted_todo` | cost and Turns per accepted todo |

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
- accepted todos, cost and Turns per accepted todo;
- the per-role split (at most 8 rows);
- the budget share, when a budget is set.

It is attached to every goal that has a ledger, peer_v1 goals included. Its
reads are bounded:
- it aggregates only the latest 5000 rows (`truncated: true` when rows were
  cut);
- accepted todos come from the status `todo_index`, which is capped;
- a missing or broken ledger just leaves the summary out.

Code: `loopx/control_plane/status/usage_projection.py`.

The dashboard's role board shows it as one strip under its header (see
[role-board-v0](role-board-v0.md#usage-strip)).

## Budget (optional)

```sh
loopx usage budget --goal G --set 50    # USD; --clear removes it; no flag shows spend vs budget
```

The budget lives in `<runtime_root>/goals/<goal>/usage-budget.json`. It is not
in the registry, so no goal-config schema changes.

On every pass, the dispatcher compares the ledger's total cost with the budget
(`loopx/dispatch/usage_budget.py`):
- Once per threshold and budget value, it opens one user todo when spend
  crosses 80% and again at 100%. If both are crossed at once, one todo covers
  both.
- The todo is a `user_action`, not a `user_gate`. A user gate blocks agent
  lanes in LoopX selection, and the budget must not stop work by itself.
- The pass reports the todo as `gates_opened` with key `usage_budget:80` or
  `usage_budget:100`.
- The dispatcher state (`budget_alerts`) remembers which alerts it opened. It
  also adopts an open alert by its text, so a lost state file does not
  duplicate it.
- Raising the budget re-arms both thresholds.

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
