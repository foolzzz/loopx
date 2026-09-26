# E2E pilot report v0 (design decision 28)

Date: 2026-09-26. Branch: `fork/e2e-fixes` (from `origin/dev` at `989b39922`).

## Result

The full loop ran on real models against two pilot repos. It covered intake,
clarification in a gate thread, plan approval in the web UI, contract-first
decomposition, dispatcher-driven development in per-todo worktrees, real
rejections and escalations, acceptance, atomic merges into the task branch,
web gate replies and resolution, and the role board. Nothing was pushed.

To get there, 17 fork bugs were fixed on this branch in 17 commits, each with regression tests, plus a docs commit.
Six steps needed a labelled manual action. Four gaps need a design or protocol
decision and are listed at the end.

The final task branches are green: `todo-api` 13 tests OK, `todo-web` 29 tests
pass. `main` is untouched in both repos.

## Setup

All paths below are relative to the pilot dir `/Users/a/ai/loopx-pilots/`.

- **State home.**
  - Runtime root: `.runtime/`, with the registry at `.runtime/registry.json` and
    `LOOPX_REGISTRY` set.
  - Central progress repo: `progress/`, a separate git repo that holds the goal
    state.
  - Code repos: `todo-api/` (repo `api`) and `todo-web/` (repo `web`). Both use
    `merge_target=task_branch`, so accepted work goes to
    `loopx-task/todo-priority`.
  - `~/.codex/loopx` was never read or written. Every command ran with
    `--no-global-sync`.
- **Code under test.** The wrapper `lx` runs `python -m loopx.cli` from the dev
  venv, with `PYTHONPATH` pointing at this worktree. `bin/loopx` is a shim to it.
- **Config.**
  - `.runtime/providers.yaml`:
    - `anthropic-login` (`oauth_cli`, claude);
    - `cpa` (`codex-cpa`, `api_key` from env `CPA_API_KEY`; no secret in files).
  - `.runtime/agents/{orch,dev,acc}.yaml`.
  - The claude agents need `--allowedTools=Bash,Read,Write,Edit,Glob,Grep`,
    because headless `dontAsk` denies Bash, and the orchestrator runs its gate
    and plan commands through Bash.
- **Preflight.** `loopx provider check` returned ok for both providers.
  `loopx agent validate` returned ok for all 3 agents.

### Models actually used

| role | agent | runtime | model | host invocations |
|---|---|---|---|---|
| orchestrator | `orch` | claude-code 2.1.282 | `claude-fable-5-1` @ medium | 5 |
| developer | `dev` | claude-code 2.1.282 | `claude-opus-4-6` @ medium, `max_concurrency` 2 | 11 |
| acceptor | `acc` | codex-cli 0.153.4 via CPA `127.0.0.1:8317` | `gpt-5.6-sol` @ medium, read-only sandbox | 10 |

- **Host invocations.** The counts are taken from
  `.runtime/goals/todo-priority/turns/*.json` entries that hold a `host_result`.
  - The dispatcher wrote 58 child runs (`.runtime/dispatch/runs/`). The extra
    runs are refused launches that never called a host; most came from bug 12,
    which is fixed.
- **CPA overrides.** The acceptor got all seven `-c` overrides from the S3 host
  args: `service_tier`, `model_provider=cpa`, and name, `base_url`, `wire_api`,
  `env_key` and `requires_openai_auth=false`.
- **Cost and tokens.** Not available. Neither adapter persists `total_cost_usd`,
  `num_turns` or token usage (gap G9).

## Scenario steps

1. **Intake: pass.**
   - Command: `lx goal create --project progress --goal-id todo-priority --doc req-todo-priority.md --repo api=…,merge_target=task_branch --repo web=…,merge_target=task_branch --agent orch=orchestrator --agent dev=developer --agent acc=acceptor --no-global-sync`.
   - Result: the orchestrator's planning todo `todo_2f87a7f3b328` and event `ec76c170a0e17d2b` (`goal_intake`).
2. **Clarification: pass, after fixes 1 to 3.**
   - The orchestrator's first Turn opened question gate `todo_5146d74b53b9` with
     five questions and recommended defaults.
   - My answers (gate reply, event `f1d7eb6007f72bac`):
     - Q1 to Q4: use the defaults. The default priority is `normal`; an invalid
       value returns 400; priority is set at create time only; the list sorts
       high > normal > low with ties by id ascending, in `render.js`; the web
       form gets a priority select.
     - Q5: decompose contract-first: an API contract todo in api; then backend
       (api) and frontend (web) todos in parallel; then a multi-repo integration
       todo (api + web).
     - Bind `dev` and `acc` on every todo, give every todo a validation command,
       and keep it small.
   - The orchestrator answered in the thread (event `1d942d374c39d8b3`) and
     proposed the plan.
   - I closed the gate through the CLI, which needs `--agent-id orch` (workaround
     W4, now documented).
3. **Plan approval: pass.**
   - The plan card is `plan_cec5f86e3341`, revision 1, event `533a488372d2a442`.
     It has four todos, in contract-first order:

     | key | todo | repos | depends on |
     |---|---|---|---|
     | contract | `todo_052d0a865d78` | api | none |
     | backend | `todo_0a03d20d711b` | api | contract |
     | frontend | `todo_4a6f1c6a209e` | web | contract |
     | integration | `todo_639fadb3ef50` | web, api | backend, frontend |

   - I approved it through the web HTTP path: `POST /api/actions/preview`
     (`gate.resolve`), then `POST /api/actions/proposal-f81c5873afb64720b6f04745c80c16e7/apply`.
     Event `52e93054b56a4fdb` records `plan_decided approve`, and the plan was
     applied.
4. **Development: pass, after fixes 4, 7 to 12, 14 and 15.**
   - The work ran through repeated `dispatch serve --once` passes and one resident
     `dispatch serve` window. That window exposed bug 12; the serve was stopped,
     fixed, and the run resumed with `--once` passes.
   - Worktrees: `.runtime/goals/todo-priority/workspaces/<todo>/<repo>` on branch
     `loopx/todo-priority/<todo>`.
   - Deliveries moved todos to `in_review`: events `f3eec31732b7354e`,
     `d509cb31acfeb97a`, `d269025d0e6a66f5`, `3ac4cd5081b5cdde`,
     `64c67f99a135103f` and others.
5. **Acceptance: pass. All rejections came from the model; none was manual.**
   - **Frontend `todo_4a6f1c6a209e`.**
     - Rejection #1 said README.md still lists "No priority field/UI". The todo
       reopened for `dev` with `reject_count=1` and the feedback stored.
     - The developer re-delivered unchanged work because the feedback never
       reached its Turn (bugs 11 and 16).
     - Rejection #2 escalated: the todo was blocked, and orchestrator todo
       `todo_621d1501ffd6` was opened.
     - The orchestrator reopened the todo. The same unchanged re-delivery led to
       a second escalation, `todo_754353f8091d`.
     - The orchestrator then split the work: it closed the frontend todo and
       created README follow-up `todo_8a37878d0345`. That follow-up was accepted
       (event `b67c29a80914ba07`).
   - **Integration `todo_639fadb3ef50`.**
     - It was rejected once (`reject_count=1`), reworked and re-delivered.
     - The acceptor then accepted it, but the merge preflight found a web
       conflict in `README.md`, `render.js` and `test/render.test.js`. The todo
       went back to `dev` with that report; this is not counted as a rejection.
     - The developer merged the task branch and resolved the conflict. The
       acceptor then accepted the todo and it merged (event `403c8be36172458d`).
   - Contract and backend were accepted on the first review (events
     `16e0a26651f12e3b` and `1a8c602f62d1184d`).
6. **Merge: pass.**
   - Accepted work merges atomically into `loopx-task/todo-priority`:
     - `todo-web`: `de2666d` (README follow-up) and `4fb8bc2` (integration), both
       merged automatically by the accept verdict.
     - `todo-api`: `4556fe7` (contract). This merge was run by hand with
       `lx workspace merge … --repo api` (workaround W2), because it was accepted
       before merge-on-accept existed.
   - The backend branch had no commits beyond the contract.
   - Pushing stayed behind a user gate. Push gate `todo_4e5787b9caf7` was answered
     in the web thread (event `7004bf54b2705dfe`) and rejected through the HTTP
     path (`proposal-8ba712a011a747c1a3c92111ccac7bac`, status `done`, outcome
     `reject`).
   - The pilot remotes point at `https://git.invalid/…` with the push URL set to
     `DISABLED-no-push`. `git for-each-ref refs/remotes` is empty in both repos.
7. **Web UI: pass.**
   - Server: `lx dashboard --host 127.0.0.1 --port 18790 --no-open` against the
     isolated runtime root. The chat bundle was built in the worktree first; it
     is gitignored.
   - Two gates were resolved through `gate.resolve` preview and apply (steps 3
     and 6), and one thread reply was sent through
     `POST /api/chat/gate-thread/reply`.
   - A headless Playwright screenshot was taken with
     `evidence/role-board-shot.mjs`.
8. **Role board: pass.**
   - JSON from `lx --format json status` (`run_history.goals[].role_board`):
     `evidence/role-board.json`.
   - Screenshots:
     - `evidence/role-board-in-review.png`: an in-review card and the done cards.
     - `evidence/role-board.png`: eight done cards, and the push gate pending
       under "Waiting on you".
9. **Escalation: pass, natural.**
   - It escalated twice through the second-rejection rule. Each time the todo was
     blocked and an orchestrator replan todo claimed by `orch` was opened. The
     orchestrator resolved both through real Fable Turns. The second one was
     settled after bug 13 was fixed.

## Bugs fixed on `fork/e2e-fixes`

Each commit has regression tests.

| # | commit | fix |
|---|---|---|
| 1 | `c3210abca` | The orchestrator prompt pins the exact CLI prefix (`--registry` / `--runtime-root`) and says how to open a gate. The planning todo is validated by `plan list --require-status applied` (new flag), and the orchestrator returns `user_action_required` while it waits. |
| 2 | `ccef6c276` | A gate whose thread awaits the orchestrator no longer blocks the orchestrator's lane (decision 10). Before, a user reply could never be answered. |
| 3 | `5ef6bbe5c` | `task_repositories` todos validate in their per-todo workspace, not the Goal repo. With a central progress home, every delivery failed with `NO TESTS RAN`. The misleading Turn error is fixed too. |
| 4 | `697d23cb0` | The acceptor lane is not held in `operator_gate` by a gate scoped to another agent, and the resolved acceptor can pin an `in_review` todo with `--todo-id`. |
| 5 | `685ada984` | The shared Turn prompt gives a reviewing acceptor its accept and reject contract. The dispatcher addendum reaches claude-code agents only. |
| 6 | `3ac04b235` | The accept verdict merges into the merge target atomically. A conflict returns the todo to its developer (decisions 18 and 22). |
| 7 | `24f5fea59` | Plan todos resume once all of their dependencies are done. Before, nothing reopened them, and `resume_when` names only one dependency. |
| 8 | `e01731fbd` | A free slot takes the lane's next executable todo when the selected todo is cooling down. |
| 9 | `b86df908d` | The role_v1 orchestrator is exempt from the peer-worktree refresh guard; it works from the state home and delivers no code. **Touches the workspace guard; please review.** |
| 10 | `3935ec5f9` | An acceptor's reject verdict settles as `outcome_progress`. Before, an untyped `outcome_gap` failed the Turn. |
| 11 | `b63869234` | The quota selected todo carries `review_feedback`, `reject_count` and the other role fields. The Turn prompt shows the feedback. |
| 12 | `bb65869a0` | No todo-less orchestrator launches. run-once refuses them, and the resident dispatcher relaunched one 25 times in about two minutes. |
| 13 | `c5e2f42de` | Escalation todos get a validator: the escalated todo must no longer be blocked (`python -m loopx.dispatch.checks`). |
| 14 | `c1dd790fb` | `workspace` CLI actions default to the todo's `task_repositories`. |
| 15 | `61c5c4859` | The dispatcher respects the Turn lane fence (one in-flight Turn per agent and goal), and todo backoff is keyed per agent. |
| 16 | `dbc3c1085` | The signed Turn envelope carries the role fields, `review_feedback` and the todo note. Before, the developer re-delivered rejected work unchanged. Together with 11, review feedback reaches the developer on every host. |
| 17 | `2b2ba133e` | An accept whose merge is blocked settles the acceptor Turn. Before, it failed with "durable completion requires … done". |
| docs | `f064d1659` | The docs for all of the above: dispatcher, gates and plans, workspaces, role_v1. |

**Verification.**

- `pytest tests/architecture tests/dispatch tests/control_plane tests/test_loopx_turn_*.py tests/test_turn_*.py tests/test_git_workspace.py`: 6564 passed, 4 skipped.
- `npm run test:control-plane`: 3093 passed, 1 failed. The failure, `a worktree venv wins over an unusable system python3`, fails the same way on the `dev` checkout; it depends on the environment.
- `scripts/generate_project_registry_io_manifest.py`: no diff.
- `examples/semantic-vocabulary-drift-smoke.py`: exit 0.

## Review fixes

Fixes from the PR review, one commit each, with regression tests.

| # | commit | fix |
|---|---|---|
| R1 | `c4b09f6c7` | Accept runs the real merge **before** it completes the todo. Before, the todo became `done` first. A merge that then failed (for example because another accept moved the task branch in between) left a done todo without its merge, and its dependents resumed. Now any failed merge (`merge_blocked`, `merge_apply_failed`) maps to the `merge_blocked` transition, which returns the todo to its developer, and dependents resume only after a completed accept. If the merge lands but the re-run validation then refuses completion, the todo stays `in_review` (transition `completion_blocked`, the merge is in the result). The merge is idempotent, so the accept can be retried, or the acceptor can reject. |
| R2 | `64ae603d7` | A `task_repositories` todo whose per-todo workspace is missing or off the todo branch fails validation with `workspace_unverified`. Before, it fell back to the Goal repo, which lacks the todo's commits, and passed spuriously. The workspace lookup now uses the caller's `--runtime-root` (the dispatcher's). Accept keys merge eligibility on the todo branch existing in the todo's repos, not on worktree directories, so an accept after the worktree was removed still merges, and a repo that lacks the branch blocks the merge. |
| R3 | `9823bd007` | An orchestrator with work that no todo carries gets a todo. This covers an `orchestrator_action_without_todo` (for example an S1-routed replan obligation) and open gates whose threads await it. The dispatcher opens one "Orchestrator action" todo (idempotent, one at a time), so the next pass launches a real Turn. Its validator is `checks gates-not-awaiting` or `checks todos-changed-since`. After two action todos for the same subject, a user gate opens instead of a third. This also closes most of G7. |
| R4 | `37199ff52` | The durable todo-note read in the Turn decision runs only on role_v1 goals. peer_v1 goals get no extra read and no envelope change. |

**Verification.**

- `pytest -q -n 8` (the full Python suite): 12619 passed, 61 skipped, 106 subtests passed.
- `npm run test:control-plane`: 3124 tests, 3093 passed, 30 skipped, 1 failed. The failure is the environment-dependent `a worktree venv wins over an unusable system python3` (G11).
- `scripts/generate_project_registry_io_manifest.py`: no diff.
- `examples/semantic-vocabulary-drift-smoke.py`: exit 0.
- The `loopx/todos.py` module ceiling rose by one line (2308 to 2309) for the runtime-root plumbing.

## Workarounds used (manual, labelled)

- **W1.** The pilot repos had no `origin`, and the kernel's delivery-workspace
  identity requires `remote.origin.url` (gap G3). I added
  `https://git.invalid/loopx-pilots/<repo>.git` with the push URL set to
  `DISABLED-no-push`.
- **W2.** Contract todo `todo_052d0a865d78`: I merged it by hand with
  `lx workspace merge --repo api`, because it was accepted before fix 6.
- **W3.** Backend and frontend: I resumed them once by calling
  `plan_cards.resume_ready_plan_todos` directly, before the dispatcher hook
  (fix 7) existed.
- **W4.** Clarification gate: closed with
  `lx todo complete --role user --decision-outcome approve --agent-id orch`.
- **W5.** Orchestrator todos `todo_2f87a7f3b328` and `todo_621d1501ffd6`: settled
  with `lx todo complete --no-follow-up --completion-identity-key …`, as the
  kernel suggested. It did not clear the orchestrator's vision-checkpoint replan
  obligation (gap G4).
- **W6.** The push decision gate was opened by the owner through the CLI
  (`--blocks-agent orch`). Nothing in the fork opens it (gap G8).

## Remaining gaps, in priority order

"Needs approval" marks a gap that touches the core protocol, the contract or the
schema, or the lease or guard semantics. None of these was changed.

- **G1 (P0, needs approval). Multi-repo delivery identity.**
  - A multi-repo Turn runs from the workspace root, which is not a git worktree.
    Delivery and verdicts land, but the post-settlement refresh fails the
    multi-agent worktree guard. The Turn then reports `failed`, quota is not
    spent, and the journal can be left `in_progress`.
  - The delivery-workspace snapshot models one repository. It needs a per-repo
    snapshot list, or an S5 workspace identity.
- **G2 (P1, needs approval). Plan acceptance criteria are lost.**
  - Plan cards store `acceptance` in the mutable todo note. The Turn completion
    overwrites the note with the developer's `next_action`, so the acceptor
    never sees the criteria; it saw only the todo text.
  - The fix is a dedicated `acceptance_criteria` todo field, which is a change to
    the coordination contract.
- **G3 (P1, needs approval). Origin-less repos.** The delivery identity needs
  `remote.origin.url`. Local-only repos need a local identity, for example a
  digest of the common git dir; W1 is the stopgap.
- **G4 (P1). The orchestrator and the upstream vision and replan machinery.**
  - A completed orchestrator todo raises vision-checkpoint and no-follow-up
    replan obligations that the orchestrator cannot settle through a Turn: the
    host route needs a todo, and `material_replan` needs a vision packet.
  - 3 orchestrator Turns failed with invalid `agent_vision_json`, and the
    obligation stays open.
  - Proposal: exempt role_v1 orchestrator todos from the vision checkpoint, or
    let a Turn result record no-follow-up.
- **G5 (P1). Parallel development in one goal.**
  - The Turn lane fence allows one in-flight Turn per agent and goal, so
    `max_concurrency` 2 does not parallelise one developer. The backend and
    frontend todos ran one after the other.
  - Short term: register several developer agent ids. Long term: a per-todo
    lane, which needs approval because it is a lease semantic.
- **G6 (P2). Superseded todos count as done.**
  - The orchestrator closed the superseded frontend todo as `done`, which resumed
    integration before the replacement merged. The integration branch was cut
    from a stale task branch, which caused the merge conflict above.
  - Dependency resume should require accepted and merged work, or the
    orchestrator should use `todo supersede`, which the prompt should say.
- **G7 (P2, mostly fixed by R3). Gate replies wake the orchestrator only if it has an open todo.**
  The dispatcher now opens an orchestrator action todo for them. Whether that
  Turn clears an upstream replan obligation is still the kernel's decision (G4).
  The `todos-changed-since` validator is coarse: a concurrent todo write by
  another agent also satisfies it.
- **G8 (P2). No push flow.** Nothing opens the push gate when a goal's work is
  merged, and approving it does not push. Both should be orchestrator or
  dispatcher steps.
- **G9 (P2). No cost or token accounting.** The claude-code and codex adapters
  drop `total_cost_usd`, `num_turns` and usage.
- **G10 (P3). Smaller items.**
  - `gate show` truncates the gate text.
  - A dispatcher decision can go stale while another Turn changes state. The
    Turn is then refused without a host call, which is harmless.
  - The orchestrator wrote its rework instructions only to the note, which
    `review_feedback` would carry better.
  - Unverified: `todo update --reject-count 0` by the orchestrator did not seem
    to persist; the frontend todo still escalated at the next rejection.
- **G11 (P3). One pre-existing TS test depends on the environment:** `a worktree
  venv wins over an unusable system python3`.
- **G12 (P2, not changed). An acceptor's `repair_required` counts as a reject.**
  An acceptor Turn that returns `repair_required` for its own reasons (for
  example broken tooling) is recorded as a reject verdict, so it increments
  `reject_count` and can escalate a todo whose delivery was not at fault.

## Evidence

All paths are relative to the pilot dir.

- `evidence/role-board.json`, `evidence/role-board.png`,
  `evidence/role-board-in-review.png`, `evidence/role-board-shot.mjs`
- `.runtime/goals/todo-priority/rollout-event-log.jsonl`: the event ids above
- `.runtime/goals/todo-priority/{gates,plans,turns,workspaces}/`
- `.runtime/dispatch/{state.json,runs/}`, `.runtime/dispatch-serve.log`
- `progress/.codex/goals/todo-priority/ACTIVE_GOAL_STATE.md`
- the task branch `loopx-task/todo-priority` in `todo-api/` and `todo-web/`
