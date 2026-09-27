# E2E pilot report v1 (regression run)

Date: 2026-09-26/27. Branch: `fork/pilot-regression` (from `origin/dev` at `aee9c4c8e`).
All paths are relative to the pilot dir `/Users/a/ai/loopx-pilots/`. Times are UTC unless
marked local (PDT).

## Result

The full loop ran again on real models, on a fresh state home and fresh repos:
- intake and clarification;
- plan approval with per-todo criteria;
- contract-first development, with backend and frontend developed in parallel by one developer;
- three natural rejects that named their failed criteria;
- one blocked verdict, resolved over the web path;
- a criteria change through a plan card, with the acceptor held while it was pending;
- a supersede;
- multi-repo acceptance and merge;
- a push to local bare remotes, with the reject path checked too.

Nine of the ten scenario steps pass. Step 7 (idle orchestrator) fails.

Most v0 gaps are confirmed fixed: G1, G2, G3, G4, G5, G8, G9, G12, and decision 40. W1–W6 were
not needed.

The run found three dispatcher bugs, all fixed on this branch with regression tests:
- A deferred todo was launched, which also cut a stale branch. This is the v0 G6 symptom by a
  new path.
- An orchestrator action todo whose Turns kept failing relaunched without end.
- The dispatcher lost its counters on restart.

It also found one design gap that needs a decision (N2): a stale Next Action wakes the
orchestrator on a finished goal.

Final task branches are green in fresh clones: api `python3 -m unittest` 26 tests OK, web
`node --test` 30 pass. `main` is untouched in both repos and both bare remotes.

## Setup

- **State home.**
  - Runtime root `.runtime2/` (fresh). Progress repo `run2/progress/` (fresh git repo).
  - Code repos `run2/todo-api` (repo `api`) and `run2/todo-web` (repo `web`), cloned from the v0
    pilot repos' `main` through local bare remotes `run2/remotes/{todo-api,todo-web}.git`, which
    are their `origin`. No fake origin: W1 was not used.
  - The local-path origin does not normalize to a remote identity, so `repo_id` falls back to
    the decision-29 local digest.
  - Both repos use `merge_target=task_branch`, so accepted work lands on `loopx-task/todo-due`.
  - `~/.codex/loopx` was not touched. Every command ran with `--no-global-sync`. Nothing was
    pushed to a network remote.
- **Code under test.**
  - `lx2` runs `python -m loopx.cli` from `/Users/a/ai/loopx-wt/pilot-regression`, with
    `--registry .runtime2/registry.json --runtime-root .runtime2`.
  - The dispatcher is `run2/dispatch.sh`: resident `dispatch serve`, `--max-global 3`,
    `--turn-timeout-seconds 1500`, tick 60 s.
  - The dashboard is `lx2 dashboard --port 18791`. The chat bundle had to be built in the
    worktree first (`npm run build:chat`; gitignored).
- **Agents.**

  | role | agent | runtime | model | notes |
  |---|---|---|---|---|
  | orchestrator | `orch` | claude-code | `claude-fable-5-1` @ medium | `--allowedTools=Bash,Read,Write,Edit,Glob,Grep` |
  | developer | `dev` | claude-code | `claude-opus-4-6` @ medium | `max_concurrency` 2, `acceptEdits` |
  | acceptor | `acc` | codex-cli via CPA | `gpt-5.6-sol` @ medium | the 7 `-c` CPA overrides in `extra_args`; no explicit sandbox, so the role default is `danger-full-access` |

- **Pricing.** `providers.yaml` gives `cpa` a `pricing` table: input 1.25, cached 0.125,
  output 10 USD per 1M tokens. **These are placeholder prices**, so the codex costs below are
  placeholder estimates.
- **Preflight.** `provider check` ok (2/2); `agent validate` ok (3/3).
  Evidence: `evidence-v1/provider-check.json`, `evidence-v1/agent-validate.json`.
- **Requirement.** `run2/req-todo-due.md`:
  - the API accepts an optional ISO `due`, rejects invalid dates with 400, and supports
    `?overdue=true`;
  - the web client shows the due date, highlights overdue todos, and can filter to overdue.

## Scenario steps

| # | step | result |
|---|---|---|
| 1 | intake, clarification, plan approval | **pass** (the clarification ran in the plan gate thread; see notes) |
| 2 | parallel development (G5) | **pass** |
| 3 | acceptance, multi-repo settle, criteria, review checkout (G1/G2/G12) | **pass**, after fixes F1 and F2 |
| 4 | blocked verdict, web retry | **pass** (manual trigger, labelled) |
| 5 | criteria change through a plan card (decision 40) | **pass** (proposed on the orchestrator's behalf, labelled) |
| 6 | supersede (G6) | **pass** (manual, labelled; no natural replacement happened) |
| 7 | idle orchestrator (G4/G13) | **fail**: G4 passes; G13 has a residual (N2) and a cost loop (fixed by F3/F4) |
| 8 | push (G8) | **pass**, reject path included |
| 9 | usage | **pass**, with a counting caveat (N5) |
| 10 | interventions | 16 by design or scaffolding, 2 workarounds (below) |

### 1. Intake, clarification, plan: pass

- **Intake.**
  - Command: `goal create --goal-id todo-due --doc run2/req-todo-due.md --repo api=…,merge_target=task_branch --repo web=…,merge_target=task_branch --agent orch=orchestrator --agent dev=developer --agent acc=acceptor`.
  - Result: planning todo `todo_203a125e4c76` (`evidence-v1/goal-create.json`).
- **Orch Turn 1 (`1790490535-48b67970`).**
  - It proposed plan card `plan_43aec6a1d043` rev 1, with gate `todo_d20650363058`. Rev 1 had
    5 todos and no contract todo.
  - It posted its clarification questions Q1–Q7, with defaults, in the plan gate thread instead
    of opening a separate question gate.
  - Everything it asked is answerable in one round, so this is acceptable. It is noted as N7.
- **H1.** As the user, I answered Q1–Q6 in the thread and asked for a contract-first, 4-todo
  revision.
- **Orch Turn 2 (`1790490848-c29e2468`).**
  - It failed at `host_execute` (`claude_code_unknown`) after 658 s.
  - It overlapped a network outage (ENOTFOUND) on the operator machine, so this is environmental.
- **Orch Turn 3 (`1790491570-374f6cf9`).**
  - It revised the card to rev 2 and answered in the thread. Rev 2 is contract-first:
    - `contract` `todo_2ca2b0abd114`, api;
    - `backend` `todo_cdd93dbe4945`, api, depends on contract;
    - `frontend` `todo_98eec0a43eb1`, web, depends on contract;
    - `integrate` `todo_ee9cf116b96c`, api + web, depends on backend and frontend. Its
      validation command runs both suites from the workspace root.
  - Every todo has `acceptance_criteria` in the dedicated field (789–1000 characters).
  - Minor: the gate text still said "(5 todos)" after the revision (N8).
- **H2.** Approved with `gate resolve --decision approve`. No `--agent-id` was needed, so W4 was
  not needed.
  - `todo list` shows `dependency_waits` for the three deferred todos.
- **Orch Turn 4 (`1790492339-93001b0d`).** The planning todo closed as `validated_completion`
  once the plan was applied (43 s).

### 2. Parallel development (G5): pass

- One pass (`at 1790492742`) launched `dev` twice:
  - backend (`1790492743-eeb8e55c`, `selected_todo`);
  - frontend (`1790492744-e97531da`, `alternate_todo`).
- The next pass reported `dev slots_full 2/2`.
- The Turn timestamps from `.runtime2/goals/todo-due/usage.jsonl` (`evidence-v1/turn-table.txt`)
  show the overlap:

  | todo | started | finished |
  |---|---|---|
  | frontend | 07:05:45 | 07:08:02 |
  | backend | 07:05:45 | 07:08:42 |

  The two Turns overlapped for **137 s** on one developer.

### 3. Acceptance: pass after fixes

**G12, review on a detached checkout.**
- Every acceptor Turn on a repo todo ran in
  `.runtime2/goals/todo-due/reviews/<todo>/<run_id>/<repo>`, detached at the delivered sha:
  - contract: `api@29e1763`;
  - integration: `api@71fdca3` and `web@15ca88f`, the two repos of
    `reviews/todo_ee9cf116b96c/1790497930-637a9912/`.
- Each review dir was removed after its reap. That includes the one from the failed acceptor
  Turn. At the end, `reviews/` is empty and `git worktree list` shows no review worktrees.
- The merge uses the delivered sha: the contract merge `818ee49` has second parent `29e1763`,
  which equals `delivered_shas=api@29e1763…`.
- The acceptor role default was used as intended (`danger-full-access`, no explicit sandbox).
  No `acceptor_modified_review_checkout` event was recorded.

**G2, criteria reach the acceptor, and rejects name the failed criteria.** There were 3
natural rejects (`repair_required`) and 0 manual ones:

| todo | event | feedback |
|---|---|---|
| frontend | `27973e95b4cc35d3` | "Failed criterion: render.js isOverdue(todo, today) must default today to the local date. It currently uses new Date().toISOString()…, which returns the UTC date…". A real bug, caught with a deterministic America/Los_Angeles boundary check. |
| backend | `ba8c838178762cce` | against the criteria changed in step 5: "Failed criterion — case-insensitive overdue handling… Failed criterion — required tests… Failed criterion — documentation…" |
| — | — | the third bounce is the integration merge conflict below, which is not counted as a reject |

- Each reject reopened the todo for `dev` with `reject_count=1`. Each rework was accepted on the
  next review:
  - frontend: `c1a0619503b81c5c`;
  - backend: `51439a815451dc53`, merge `1d65194`.
- Caveat: the feedback is cut at about 400 characters (N6).

**G1, multi-repo Turns settle.**
- Integration dev Turn `1790497605-9623190e` (api + web):
  - `status=committed`, `validated_completion`;
  - `quota_slot_spend_count` 1;
  - journal `turn:6b4998ea329b915c` committed;
  - delivery `delivered_shas=api@71fdca3,web@15ca88f`.
- At the end, every journal in `.runtime2/goals/todo-due/turns/` is closed: `committed`,
  `stopped` or `failed`, and none is `in_progress`.
- No failed refresh anywhere in the run.

**Contract acceptor.**
- The first contract acceptor Turn (`1790492566-de7f6880`) failed. CPA returned
  `503 auth_unavailable` because its upstream dial failed during the outage. This is
  environmental.
- The adapter classified it `unknown` and non-retryable (N9).
- The retry accepted the contract: `99b3478cf6d32248`, merge `818ee49`.

**Bug F1 (fixed), a deferred todo was launched.**
- Once frontend was accepted, upstream should-run selected the still-deferred integration todo
  for `dev`, as `successor_replan_required`. Its `deferred_resume_candidates` checks only
  `resume_when=todo_done:<frontend>`, and ignores the decision-37 rule that backend must also be
  accepted and merged.
- The dispatcher launched it pinned 7 times. Run-once refused each launch without a host call:
  "Requested Turn Todo is not currently eligible". Meanwhile the todo's backoff grew to about an
  hour.
- Worse, the first of those launches had already run workspace prepare, at 00:10:59 local. That
  cut `loopx/todo-due/todo_ee9cf116b96c` from `818ee49`, which held only the contract, 76 min
  before backend merged.
- The developer then re-implemented `due` in api on the stale branch. The acceptor accepted the
  integration, but the merge was blocked: `api: merge_conflict (test_todo_api.py, todo_api.py)`.
  The todo went back to `dev`, not counted as a reject.
- The developer rebuilt the branch on the current task branch and re-delivered. It was accepted
  (`245697e2cc73d858`) and merged: api `c5e386e`, with only the smoke script `df42736` on top of
  `1d65194`. Web had no own commits.
- This is the v0 G6 stale-branch symptom again, by a new path.
- Fix: `04a4b0dec` (dispatcher) and `e175bbb1c` (test). After the fix, the live dispatcher
  skipped `dev` as `selected_todo_deferred`, with the `dependency_wait` text, until backend was
  accepted.
- W1 cleared the stale backoff.

### 4. Blocked verdict: pass (manual trigger, labelled)

- **H3, a scaffolding trigger.**
  - After the contract delivery, I paused the dispatcher with SIGSTOP. Children run in their own
    session, so they kept running.
  - I then ran `todo block-review --agent-id acc --reason "PILOT-MANUAL …"`, event
    `b1f071f20ef7a64b`.
  - Result: `transition=review_blocked`, the todo stayed `in_review`, `reject_count` was
    unchanged, and system gate `todo_afacad641390` opened with four options:
    `retry_acceptance`, `accept_manually`, `return_to_developer`, `cancel_todo`.
    Evidence: `evidence-v1/block-review.json`.
- **H4.** Resolved over HTTP:
  - `POST /api/actions/preview` (`gate.resolve`, `option=retry_acceptance`) returned
    `proposal-62d0123d9c0641058fa64d542537e236`.
  - `POST …/apply` returned `outcome=gate_resolved`, `decision_option=retry_acceptance`,
    `surface=local_dashboard`.
  - Evidence: `evidence-v1/web-gate-resolve-{preview,apply}.json`.
  - The preview body needs `action_kind`, `summary`, `idempotency_key`, `context` and
    `normalized_parameters`. Apply needs the body `{}`.
- The acceptor reviewed again on the next tick. The blocked verdict never counted as a reject.

### 5. Criteria change (decision 40): pass (proposed on the orchestrator's behalf, labelled)

- **H5, direct edit.** `todo update --agent-id orch --acceptance-criteria …` was refused with
  `acceptance_criteria_change_requires_plan`, and the message points to `plan propose`.
  Evidence: `evidence-v1/criteria-direct-edit-refused.json`.
- **H6, the proposal.** I ran `plan propose --agent-id orch` with one `criteria_changes` entry for
  backend: "the overdue flag is matched case-insensitively". This produced card
  `plan_97a14f7cefe4`, gate `todo_50eb0cf14986`. Evidence: `run2/criteria-change-plan.json`.
- **Hold.** Backend was delivered while the card was pending, and the acceptor was held:
  - every pass skipped `acc` with `criteria_change_pending_todo_ids=[todo_cdd93dbe4945]`;
  - should-run shows `role_scope.review_held.reason=criteria_change_pending`;
  - the role-board card carries `criteria_change_plan_id` and `criteria_change_gate_todo_id`.
  - Evidence: `evidence-v1/criteria-hold-acc-should-run.json`,
    `evidence-v1/status-during-criteria-hold.json`.
- **H7, approval.** The result was `criteria_change_results=applied`, and event
  `5d6056ddf57341e6` (`todo_criteria_change`, `approved_by=user`, class major, digests only). The
  new criteria took effect: the acceptor then rejected backend against exactly the new criterion
  (step 3).

### 6. Supersede (G6): pass (manual, labelled)

No natural replacement happened, so I ran one throwaway check with the dispatcher stopped, so
nothing executed.

- H11 approved plan `plan_47add9ed5f96`, which has `s-old` `todo_cd6488c157b4` and `s-dep`
  `todo_a1c0cd76d94b` (depends on `s-old`).
- H12 added replacement `todo_2f22768c0fbf`.
- H13 ran `todo supersede --todo-id todo_cd6488c157b4 --by todo_2f22768c0fbf --agent-id orch`:
  - it rewired `todo_a1c0cd76d94b`, with event `e64b8e6a97eae8c9` and a line in
    `plans/supersessions.jsonl`;
  - the dependent's `dependency_wait` and `resume_when` now name the replacement.
- H14 closed the replacement without a replacement of its own. The dependent keeps waiting:
  "superseded without a replacement".
- H15 closed the throwaways with plain `todo supersede --agent-id dev`: `ee3fec71fb8493dd` and
  `033a96c0b54820b8`.
- Evidence: `evidence-v1/supersede*.json`, `evidence-v1/supersede-dependency-waits.txt`.
- Friction:
  - `todo add --agent-id orch` is refused for agent todos, so the orchestrator must add a
    replacement without an agent id;
  - plain supersede needs the claim owner as `--agent-id`.

### 7. Idle orchestrator (G4/G13): fail (residual gap N2; cost loop fixed)

**G4 holds.** No Turn failed on vision or replan: 0 dispatcher lines mention vision or
`autonomous_replan_required`, and no role_v1 obligation was raised.

**Orchestrator Turns: 8 launches, 6 recorded in the usage ledger.**

| # | run | todo | why it launched | outcome |
|---|---|---|---|---|
| 1 | `1790490535-48b67970` | planning | intake todo | plan rev 1 and questions, `user_action_required`, $1.96 |
| 2 | `1790490848-c29e2468` | planning | my thread reply (awaiting orchestrator) | host failure during the network outage, $1.09 |
| 3 | `1790491570-374f6cf9` | planning | same, retry | plan rev 2, $2.37 |
| 4 | `1790492339-93001b0d` | planning | plan applied, closeout | `validated_completion`, $0.85 |
| 5 | `1790498498-95f5afcb` | action `todo_97324a587600` | `state_projection_gap_repair` from a stale Next Action (N2) | opened "Goal complete: confirm closure" gate `todo_51978cd75fc1`, $2.06 |
| 6 | `1790499193-59abbd46` | same action todo | relaunched after the closure gate was approved (H16) | `validation_failed` (nothing left to change), $2.56 |
| 7, 8 | `1790499464-a79ddaa2`, `1790499839-f3049743` | same | relaunch loop | killed by me to cap spend; not in the ledger |

**The waste in rows 5–8 comes from two problems.**

- **N2, the trigger (needs a decision).**
  - Once no agent todo was open, should-run raised `next_action_executable_without_agent_todo`.
  - It read the goal's Next Action, which the **last acceptor Turn** had written: "Settle
    todo_ee9cf116b96c as accepted; no developer repair is required."
  - Decision 39's survey had left this check as a watch item. It fired on this pilot.
  - Evidence: `.runtime2/goals/todo-due/runs/2026-09-27T01-39-25-07-00.json`
    (`state_projection_gap.first_evidence`).
- **F3/F4, the loop (fixed).**
  - The action todo's `todos-changed-since` validator cannot pass when nothing is left to do, so
    the todo stayed open. It relaunched a real Fable Turn (about $2.5) on every backoff expiry.
  - The repeat limit only counts action todos that *finish*, and the dispatcher also lost its
    counters on every restart.
  - Fixed by `298e72ed4` and `99cf184d4`: after two failed Turns the dispatcher closes the action
    todo and opens the repeat-limit gate.
  - Verified live: the pass reported `orchestrator_action_retired` with `failed_turns` 2, closed
    `todo_97324a587600`, opened gate `todo_35892848f434` (left open), and launched nothing
    afterwards.
  - W2 seeded the counter for this verification.

**Pointless gates.** The closure gate `todo_51978cd75fc1` is borderline: it summarises the goal
well, but it is a second human touchpoint right after the push approval, and it cost a Fable
Turn. No other pointless gate or action todo appeared.

### 8. Push (G8): pass, with the reject path

- The dispatcher opened push gate `todo_8ea92a0e21cb` once everything had merged
  (`8c4c3390817369ee`, `requested_by=dispatcher`):
  - api: 7 commits, `(new branch)..c5e386e`;
  - web: 3 commits, `(new branch)..15ca88f`.
  - `gate show` JSON lists `push_repos` with the logs.
- **H8, reject.** Rejected through the CLI:
  - it recorded `push_declined` (`e693a20524147446`);
  - `push/state.json` holds the declined heads;
  - nothing was pushed, and the gate was not re-offered on the following passes.
- **H9.** Ran the owner's `goal request-push`, which opened gate `todo_517ab41fb3cf`
  (`e5afd6932241439b`).
- **H10, approve.** Approved through the web `gate.resolve`
  (`proposal-9160e3d8a96547f48569257b1ca26fbd`):
  - `push_result ok` for api (`c5794b4338d93b5e`) and web (`0be5a5ff21423aa8`);
  - both bare remotes now have `loopx-task/todo-due` at the approved heads;
  - `main` is unchanged, and nothing was forced.
- Evidence: `evidence-v1/push-gate-1*.json`, `evidence-v1/request-push.json`,
  `evidence-v1/web-push-{preview,apply}.json`.

### 9. Usage: pass, with a counting caveat

`loopx usage report --goal todo-due --by role` and `--by todo`, plus `--by model` and
`--by agent`, are saved as `evidence-v1/usage-by-*.{json,txt}`.

| role | Turns | agent-h | cost | of which estimated |
|---|---|---|---|---|
| orchestrator (Fable) | 6 | 0.48 | $10.89 | – |
| developer (Opus 4.6) | 7 | 0.28 | $5.99 | – |
| acceptor (gpt-5.6-sol) | 8 | 0.13 | $0.66 | $0.66 (placeholder pricing) |
| **total** | **21** (3 failed) | **0.89** | **$17.54** | **$0.66** |

- **Reported vs estimated.** $16.88 was host-reported by claude-code, and $0.66 was estimated
  from the placeholder CPA prices. One acceptor Turn is unpriced (the CPA 503 left only a
  wall-clock time). The two killed orchestrator Turns are not in the ledger.
- **Tokens.** 13.2M in total, of which 12.0M were cached input.
- **Per todo.**

  | todo | cost | Turns |
  |---|---|---|
  | planning | $6.27 | 4 |
  | integration | $3.14 | 4 |
  | action todo (N2) | $4.62 | 2 |
  | backend | $1.45 | 4 |
  | frontend | $1.28 | 4 |
  | contract | $0.79 | 3 |

- **Cost per accepted todo.**
  - The report says `accepted_todos=6`, $2.92 per todo, 3.5 Turns per todo.
  - Only 4 todos went through acceptance, so the real figure is **$4.39 per accepted todo**
    ($17.54 / 4), or $1.93 without the orchestrator.
  - The report counts every todo that is done now, which includes the orchestrator's planning
    todo and the retired action todo (N5).
- **Status payload.** `run_history.goals[].turn_usage_summary` is present, with the `by_role`
  split (`evidence-v1/turn-usage-summary-status.json`). The role-board screenshot is
  `evidence-v1/role-board.png` (script: `evidence-v1/role-board-shot.mjs`).

### 10. Human interventions

**Expected by design (13):**

| # | intervention |
|---|---|
| H1 | plan thread reply |
| H2 | plan approve |
| H4 | web retry of the blocked gate |
| H7 | criteria card approve |
| H8 | push reject |
| H9 | request-push |
| H10 | web push approve |
| H16 | closure gate approve |
| H5, H11, H12, H13, H14 | probes and scaffolding that the scenario asked for: the direct-edit probe, and the supersede plan, add, supersede and check |

**Scaffolding for the pilot (3), labelled:**

| # | intervention |
|---|---|
| H3 | `block-review`, with the dispatcher paused by SIGSTOP |
| H6 | the criteria-change proposal run on the orchestrator's behalf |
| H15 | cleanup of the throwaway todos |

**Workarounds (2):**

| # | workaround | why |
|---|---|---|
| W1 | removed the bug-caused backoff entry `todo-due/todo_ee9cf116b96c@dev` (7 failures) from `.runtime2/dispatch/state.json` (copy: `evidence-v1/dispatch-state-before-W1.json`) | F1 |
| W2 | seeded `orchestrator_action_failures` = 2 for `todo_97324a587600` (1 real validation failure, 1 killed Turn; copy: `evidence-v1/dispatch-state-before-W2.json`) | live-verify F3 without another Fable Turn |

**Operational (not state changes):**
- 3 dispatcher restarts, to load the fixes;
- 2 orchestrator Turns killed to cap spend;
- the chat bundle built for the dashboard.

## Comparison with v0

| v0 gap | v1 |
|---|---|
| G1 multi-repo delivery identity | **fixed**: the multi-repo Turn committed, quota spent, journal closed, no failed refresh |
| G2 criteria lost | **fixed**: criteria are in the field on every todo, and rejects name the failed criteria (truncation caveat: N6) |
| G3 origin-less repos | **fixed / not needed**: no fake origin; a local-path origin falls back to the local `repo_id` |
| G4 vision and replan obligations | **fixed**: no vision or replan failure in 21 Turns |
| G5 serial developer | **fixed**: 137 s overlap on one developer |
| G6 superseded counts as done | **fixed** for supersede (step 6). **Regressed by a new path**: the dispatcher launched and prepared a deferred todo (F1), which recreated the stale-branch merge conflict. Fixed here |
| G7 gate replies wake the orchestrator | **works**: the reply woke the orchestrator on its planning todo |
| G8 push flow | **fixed**: gate, reject, request and approve; pushed to local bare remotes |
| G9 usage | **fixed**, with caveat N5 |
| G10 small items | `todo update --review-feedback` was not exercised. The gate text staleness is still there (N8) |
| G11 environment TS test | not rerun (Python-only changes) |
| G12 acceptor isolation and verdicts | **fixed**: detached review at the delivered sha, cleanup, sha-pinned merge, blocked verdict with options |
| G13 idle orchestrator | **partly regressed**: the watch item `next_action_executable_without_agent_todo` fired on the finished goal (N2), and it fed an unbounded relaunch loop (fixed: F3/F4) |
| decision 40 | **works**: direct edit refused, card applied, acceptor held |
| W1–W6 | none needed |

## Fixes on this branch

Each fix has regression tests that fail without it.

| # | commit | fix |
|---|---|---|
| F1 | `04a4b0dec`, test `e175bbb1c` | The dispatcher never launches a deferred todo that should-run offers for a role_v1 todo lane. It fills the slot with an executable alternate, or skips with `selected_todo_deferred` and the todo's `dependency_wait`. No backoff accrues, and no workspace is prepared, so no stale branch is cut. |
| F3 | `298e72ed4` | A dispatcher "Orchestrator action" todo whose orchestrator Turns fail twice (`failed`; host and provider failures do not count) is retired. It is closed through the ordinary supersede, attributed to its claim owner, and one repeat-limit user gate opens and holds the orchestrator. The pass reports `orchestrator_todos_retired` and skips with `orchestrator_action_retired`. |
| F4 | `99cf184d4` | `load_state` kept only the keys listed in `empty_state`, so `orchestrator_actions` (the repeat counter), `orchestrator_action_failures` and `budget_alerts` were dropped on every dispatcher restart. They are now kept: additive keys, same state schema version. |

No core protocol, DB schema, lease or guard semantics were changed.

**Verification.**
- `tests/dispatch` + `tests/architecture`: 854 passed.
- `scripts/generate_project_registry_io_manifest.py`: no diff.
- Full suite `python -m pytest -q -n 8 -p no:cacheprovider`: 12814 passed, 61 skipped, 106 subtests passed.

## New gaps, in priority order

- **N2 (P1, needs a decision). A stale Next Action wakes the orchestrator on a finished role_v1
  goal.**
  - `next_action_executable_without_agent_todo` fires once every agent todo is done.
  - Under role_v1, the goal's Next Action is written by whichever Turn settled last, typically
    the acceptor ("Settle todo_X as accepted…").
  - Cost here: one action todo, one Fable Turn and a closure gate that repeats the push approval.
    With F3 that is now bounded to at most 2 Turns plus a gate per recurrence. Without F3 it was
    unbounded.
  - Options:
    - (a) extend decision 39: for role_v1, drop that evidence too, because the push gate
      (decision 38) already marks the end of the work;
    - (b) do not let developer and acceptor Turns write the goal Next Action under role_v1;
    - (c) have the dispatcher open a deterministic "goal complete" gate after the push, with no
      Fable Turn.
  - Recommendation: (a) plus (c). This changes the semantics of an approved decision, so it
    needs the user's decision.
- **N3 (P2). The status `todo_index` goes stale for CLI lifecycle writes.** The role board reads
  the status `todo_index` (source `attention_queue_and_rollout_event_log`). After the run it
  still showed:
  - the approved closure gate `todo_51978cd75fc1` as `open`;
  - the superseded `todo_cd6488c157b4` as `deferred`;
  - superseded throwaways as done cards, although the role board doc says superseded todos are
    left out.

  The goal state file is correct. The fold of `gate resolve` and `supersede --by` events looks
  incomplete. It is in the event-log and replay path (core-adjacent), so it was not changed here.
- **N5 (P2). `usage report` "accepted" counts every todo that is done now**, including the
  orchestrator's planning todo and dispatcher action todos. Cost per accepted todo is understated
  (reported $2.92, real $4.39). Recommendation: count todos with an accept record
  (`accepted_by=`), and show orchestrator spend separately.
- **N6 (P2). Reject feedback is cut at about 400 characters.**
  - `review_feedback` allows 600, but the acceptor's summary arrives cut at about 400 characters
    (422 with the prefix).
  - The backend reject lost its last criterion ("README.md and the pre…"). The frontend feedback
    ends in stray host glyphs.
  - Recommendation: carry the verdict feedback in its own bounded result field, or raise the
    summary limit for acceptor verdicts.
- **N9 (P2). A CPA upstream 503 is classified `unknown` and non-retryable.**
  - The codex adapter discards stderr, so `503 auth_unavailable … dial upstream` became a todo
    backoff instead of a provider backoff.
  - Recommendation: map an upstream 5xx or `auth_unavailable` from CPA to `provider_capacity`.
- **N10 (P3). A released plan todo keeps a branch cut earlier.** F1 removes the path seen here.
  Any other early prepare (for example a manual `workspace prepare`) would still leave a stale
  branch. Recommendation: on release, fast-forward an untouched todo branch to the current merge
  target.
- **N7 (P3).** The orchestrator put its clarification questions into the plan gate thread instead
  of a question gate. That worked here, but it means "clarify, then plan" is one gate, not two.
- **N8 (P3).** The plan gate text keeps the rev-1 todo count after `--revise` ("(5 todos)" for a
  4-todo card).
- **N11 (P3).**
  - Orchestrator ergonomics: `todo add --agent-id orch` is refused for agent todos, and plain
    `todo supersede` needs the claim owner as `--agent-id`.
  - The dashboard needs `npm run build:chat` in a fresh worktree.
  - `dispatch serve` logs only passes that act, so an idle goal looks silent.

## Evidence

All paths are relative to the pilot dir.

- **Preflight:** `evidence-v1/provider-check.json`, `agent-validate.json`, `goal-create.json`.
- **Status and board:**
  - `evidence-v1/status-final.json`, `role-board-final.json`, `role-board.png`,
    `role-board-shot.mjs`;
  - `turn-usage-summary-status.json`, `status-during-criteria-hold.json`.
- **Usage:** `evidence-v1/usage-by-{role,todo,agent,model}.{json,txt}`, `turn-table.txt`.
- **Gates and verdicts:**
  - `evidence-v1/block-review.json`, `web-gate-resolve-*.json`;
  - `criteria-*.json`, `supersede*`, `push-gate-1*.json`, `request-push.json`,
    `web-push-*.json`, `closure-gate-approve.json`.
- **Should-run:** `evidence-v1/criteria-hold-acc-should-run.json`,
  `orch-should-run-after-done.json`.
- **Dispatcher state:** `evidence-v1/dispatch-state-before-W1.json`,
  `dispatch-state-before-W2.json`.
- **Runtime:**
  - `.runtime2/goals/todo-due/{rollout-event-log.jsonl,usage.jsonl,gates,plans,turns,push,workspaces}`;
  - `.runtime2/dispatch/{state.json,runs/}`, `.runtime2/dispatch-serve.log`.
- **Repos:**
  - `run2/progress/.codex/goals/todo-due/ACTIVE_GOAL_STATE.md`;
  - `loopx-task/todo-due` in `run2/todo-api`, `run2/todo-web` and `run2/remotes/*.git`.
