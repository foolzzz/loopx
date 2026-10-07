# Quota CLI Hot-Path Compaction v0

`quota_cli_hot_path_compaction_v0` bounds the default agent-facing
`quota should-run` projection without changing the decision computed by the
quota control plane. The full decision is built first. CLI-only projection then
retains action authority on the hot path and moves repeated diagnostic detail
behind explicit `--include-detail` selectors.

## Ownership Boundary

The quota control plane owns decision, precedence, scheduler, interaction,
selected-todo, and user-action semantics. `cli_projection.py` owns only the
serialized view consumed by agents. A compactor must not become a second
decision owner or recompute any route.

The default projection retains:

- `decision`, `should_run`, `effective_action`, and `recommended_action`;
- selected todo, bounded `action_portfolio`, read-only `planning_horizon`, and
  execution obligation;
- interaction mode, user channel, and executable agent/CLI actions;
- scheduler action and autonomous-replan authority;
- the compact vision decision, trigger kinds, required reads, and judge result;
- warning kinds, counts, stable identities, and cold-path references.

`action_portfolio` is not diagnostic candidate noise. It is retained in the
default packet because it carries the executable fallback rule when the
selected primary becomes unavailable at its real call site. Compaction may
remove the larger todo/capability candidate lists only after preserving this
bounded portfolio unchanged.

The `turn_envelope_action_dimensions_v2` base/head migration has a JSON-only,
bounded growth allowance for this additive portfolio. The allowance applies
only while a v0/v1 baseline migrates to v2, remains a review signal, and still
fails above 1,280 characters/bytes, 36 lines, or 896 compact characters. Once
v2 is the baseline, the ordinary hot-path growth limits apply again.

`quota_planning_horizon_v0` is likewise action-bearing context rather than
diagnostic noise. The compact path preserves its bounded Todo chain, typed
relations, attention ids, completeness counters, and cold-path refs unchanged.
Its `turn_envelope_action_dimensions_v3` migration receives one JSON-only
allowance of 3,200 characters/bytes, 84 lines, or 2,800 compact characters.
That allowance applies only to `none -> quota_planning_horizon_v0` together
with v0/v1/v2 action coverage moving to v3. Once v3 is the baseline, ordinary
growth limits resume. The horizon remains read-only and never replaces
`selected_todo` or explicit action-portfolio selection.

The hot-path horizon and `--include-detail agent-todos` share the same
TypeScript-owned `todo_planning_inventory_v0`; they are not aliases. The former
actively discloses at most five strategic items. The latter adds the larger
`todo_planning_inventory_detail_v0` lens, including planning state, claim state,
typed relations, and completeness, while referring to the existing Todo
summary for repeated item details. A concrete `todo list --goal-id ... --role
agent --status open --agent-id ...` command remains the complete source read.
Inventory overflow must become explicit incompleteness, not a quota failure or
an unbounded default packet.

The additive `none -> todo_planning_inventory_detail_v0` migration has a
JSON-only allowance of 1,280 characters/bytes, 36 lines, and 1,024 compact
characters. It applies only when the probe observes that exact schema change on
the explicit detail variants. Unknown schemas and larger growth fail closed;
once v0 is in the base, ordinary cold-path limits resume.

Repeated vision audits use `$.vision_continuation_audit` as the canonical
projection. Candidate lists and peer action lists retain counts and point to
`--include-detail agent-todos`. The complete vision audit is available through
`--include-detail vision`; `--include-detail all` restores every supported
detail section.

When a replan action carries a complete `vision_authoring` schema, the default
`quota should-run` packet keeps its executable writeback summary (`required_fields`,
accepted path outcomes, and rule) and replaces only that nested schema with a
`vision_authoring_detail_ref`. `--include-detail vision` restores the schema.
`turn plan` is different: its TurnEnvelope preserves the complete schema because
the plan must be capable of authoring the exact input its validator accepts.
The crowded Turn budget therefore accounts for that fixed contract without
relaxing Todo-count growth or the small and multi-Agent ceilings.

## Qualification Contract

Deterministic tests own exact full-versus-compact parity, cold-path restoration,
schema shape, and the character budget. The real-scale regression must exceed
the default budget before compaction and remain within it afterward.

The provider-neutral `actual_default_model_behavior_portfolio_v0` accepts
caller-supplied actors and checks the CLI hot-path projection against independent
source oracles. Scripted fixture results prove harness and semantic invariants;
they do not establish live model or tool behavior. The bundled Doubao adapter
and live qualification commands have been removed.

Packets, prompts, raw responses, credentials, and conversations remain outside
the repository.
