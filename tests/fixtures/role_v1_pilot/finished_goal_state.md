---
status: active
owner_mode: goal
objective: "Todo due dates across API and web"
updated_at: 2026-09-27T21:10:42-07:00
adapter_id: todo-due
---

# Active Goal State

## Objective

> Todo due dates across API and web

## Authority Sources

- Primary goal document: `docs/goals/todo-due/req-todo-due.md`

## Operating Contract

- Treat this file as the durable goal state for future agent ticks.
- Treat the authority sources above as the first context to inspect before acting.
- Read current project evidence before choosing the next action.
- Run a bounded progress segment when useful; it does not have to be one tiny step.
- Keep private evidence, credentials, local paths, and raw logs out of public commits.
- End each tick with changed files, validation, residual risk, and the next action.

## Execution Profile

- `cadence=bounded_progress_segment minimum=multi_surface_or_implementation include=coherent_artifact,targeted_validation,state_writeback spend_rule=spend_only_after_artifact_validation_writeback small_streak_threshold=2`
- Repeated small-scale follow-through should expand the next delivery batch or report a blocker before spending quota.

## Non-Goals

- Do not perform irreversible production operations without explicit approval.
- Do not publish private project evidence.
- Do not optimize for activity if no useful artifact or decision can be produced.

## User Todo / Owner Review Reading Queue

- [x] Approve plan: Todo due dates across API and web (v1) (5 todos) [plan_43aec6a1d043]
  <!-- loopx:todo todo_id=todo_d20650363058 status=done task_class=user_gate decision_outcome=approve bound_agent=orch blocks_agent=orch completion_continuation=no_followup no_followup=true note=approved%20rev%202 completed_at=2026-09-26T23:58:56-07:00 updated_at=2026-09-26T23:58:56-07:00 completion_turn_key=local_completion_0bc4998518edf79d4b4ed9cb0d52e654 -->
- [x] Acceptor blocked: acc cannot review todo_2ca2b0abd114 ([P1] todo-api: write CONTRACT.md pinning the `due` field, the 400 rules and the overdue filter semantics). Reason: PILOT-MANUAL (v1 step 4): simulated acceptor environment failure - review toolchain unavailable; cannot review. Choose one: retry acceptance (after fixing the environment), accept manually, return to developer, or cancel ...
  <!-- loopx:todo todo_id=todo_afacad641390 status=done task_class=user_gate decision_outcome=approve bound_agent=acc blocks_agent=acc completion_continuation=no_followup no_followup=true note=PILOT%20v1%20step%204:%20environment%20restored%2C%20retry%20acceptance completed_at=2026-09-27T00:01:47-07:00 updated_at=2026-09-27T00:01:47-07:00 completion_turn_key=local_completion_90cb9070f9d293311e159f70409739d5 -->
- [x] Approve plan: Backend: case-insensitive overdue flag (0 todos, 1 acceptance-criteria change) [plan_97a14f7cefe4]
  <!-- loopx:todo todo_id=todo_50eb0cf14986 status=done task_class=user_gate decision_outcome=approve bound_agent=orch blocks_agent=orch completion_continuation=no_followup no_followup=true note=approve%20the%20case-insensitive%20overdue%20criteria completed_at=2026-09-27T01:21:52-07:00 updated_at=2026-09-27T01:21:52-07:00 completion_turn_key=local_completion_44f0411cb943e3f15092e53d3d587b1c -->
- [x] Push request: push todo-due's merged work to its remotes? api: loopx-task/todo-due -> origin (7 commit(s), (new branch)..c5e386e71aea); web: loopx-task/todo-due -> origin (3 commit(s), (new branch)..15ca88f85bc0). Approve pushes each merge target (plain git push, never force); reject keeps it local until new merges arrive; cancel dismisses. `loopx gate show --goal-id todo-due --todo-id ...
  <!-- loopx:todo todo_id=todo_8ea92a0e21cb status=done task_class=user_gate decision_outcome=reject bound_agent=orch blocks_agent=orch completion_continuation=no_followup no_followup=true note=PILOT%20v1%20step%208:%20reject%20path%20check completed_at=2026-09-27T01:40:33-07:00 updated_at=2026-09-27T01:40:33-07:00 completion_turn_key=local_completion_8a81622b72b709c91550fa7f7e4f71ce -->
- [x] Push request: push todo-due's merged work to its remotes? api: loopx-task/todo-due -> origin (7 commit(s), (new branch)..c5e386e71aea); web: loopx-task/todo-due -> origin (3 commit(s), (new branch)..15ca88f85bc0). Approve pushes each merge target (plain git push, never force); reject keeps it local until new merges arrive; cancel dismisses. `loopx gate show --goal-id todo-due --todo-id ...
  <!-- loopx:todo todo_id=todo_517ab41fb3cf status=done task_class=user_gate decision_outcome=approve bound_agent=orch blocks_agent=orch completion_continuation=no_followup no_followup=true note=PILOT%20v1%20step%208:%20push%20to%20the%20local%20bare%20remotes completed_at=2026-09-27T01:42:01-07:00 updated_at=2026-09-27T01:42:01-07:00 completion_turn_key=local_completion_c0b48d782491c0448eb0d58209fb75f1 -->
- [x] Approve plan: PILOT v1 step 6: supersede check (throwaway, never executed) (2 todos) [plan_47add9ed5f96]
  <!-- loopx:todo todo_id=todo_78fa7140fa62 status=done task_class=user_gate decision_outcome=approve bound_agent=orch blocks_agent=orch completion_continuation=no_followup no_followup=true note=PILOT%20v1%20step%206%20throwaway%20plan completed_at=2026-09-27T01:47:08-07:00 updated_at=2026-09-27T01:47:08-07:00 completion_turn_key=local_completion_6cdce8bd5776520f40a7a73344be7d4a -->

## Agent Todo

- [x] Clarify the requirements in docs/goals/todo-due/req-todo-due.md with the user through gate threads, then propose the initial plan card (loopx plan propose)
  <!-- loopx:todo todo_id=todo_203a125e4c76 status=done action_kind=plan claimed_by=orch required_role=orchestrator requires_acceptance=false completion_continuation=active_goal note=Scheduler:%20run%20dev%20on%20todo_2ca2b0abd114%20%28write%20CONTRACT.md%20in%20api%2C%20validation%20%60test%20-f%20CONTRACT.md%60%2C%20acceptor%20acc%29. evidence=LoopX%20Turn%20validated%20completion:%20Planning%20todo%20todo_203a125e4c76%20is%20complete:%20gate%20todo_d20650363058%20thread%20shows%20user%20answers%20to completed_at=2026-09-26T23:59:44-07:00 updated_at=2026-09-26T23:59:44-07:00 completion_turn_key=dispatch:690b2884d16efd88c9988c4637269d9f -->
- [x] [P1] todo-api: write CONTRACT.md pinning the `due` field, the 400 rules and the overdue filter semantics
  <!-- loopx:todo todo_id=todo_2ca2b0abd114 status=done claimed_by=dev required_role=developer requires_acceptance=true acceptor_agent=acc task_repositories=api delivered_by=dev acceptance_criteria=CONTRACT.md%20exists%20at%20the%20todo-api%20repo%20root%20and%20pins%2C%20each%20under%20its%20own%20heading:%20%281%29%20Todo%20shape:%20every%20todo completion_continuation=active_goal note=accepted_by%3Dacc:%20The%20orchestrator%20may%20mark%20Todo%20todo_2ca2b0abd114%20accepted%20and%20continue%20the%20goal%20workflow%3B%20no%20developer evidence=accepted_by%3Dacc:%20The%20orchestrator%20may%20mark%20Todo%20todo_2ca2b0abd114%20accepted%20and%20continue%20the%20goal%20workflow%3B%20no%20developer completed_at=2026-09-27T00:05:40-07:00 updated_at=2026-09-27T00:05:40-07:00 completion_turn_key=dispatch:b9e196d8b55a07554d3b2a0be65c7e1a validation_command=test%20-f%20CONTRACT.md -->
- [x] [P1] todo-api: accept and validate optional `due` on POST /todos, return it on every todo, add GET /todos?overdue=true, with tests
  <!-- loopx:todo todo_id=todo_cdd93dbe4945 status=done claimed_by=dev required_role=developer requires_acceptance=true acceptor_agent=acc reject_count=1 task_repositories=api delivered_by=dev review_feedback=rejected%20by%20acc%20%28%231%29:%20Rejected.%20Failed%20criterion%20%E2%80%94%20case-insensitive%20overdue%20handling:%20todo_api.py%20compares%20only%20exact acceptance_criteria=todo_api.py%20implements%20CONTRACT.md:%20every%20todo%20dict%20includes%20%60due%60%20%28string%20or%20null%29%20in%20GET%2C%20POST%20and%20PATCH%20responses. completion_continuation=active_goal note=accepted_by%3Dacc:%20Settle%20Todo%20todo_cdd93dbe4945%20as%20accepted%3B%20no%20developer%20repair%20is%20required. evidence=accepted_by%3Dacc:%20Settle%20Todo%20todo_cdd93dbe4945%20as%20accepted%3B%20no%20developer%20repair%20is%20required.%3B%20LoopX%20Turn%20validated completed_at=2026-09-27T01:26:43-07:00 updated_at=2026-09-27T01:26:43-07:00 completion_turn_key=dispatch:52db21f9f29d5c191a82e22e5b7d518b validation_command=python3%20-m%20unittest%20-q -->
- [x] [P1] todo-web: render due dates, highlight overdue todos, add a date input and an 'overdue only' filter via api.js, with tests
  <!-- loopx:todo todo_id=todo_98eec0a43eb1 status=done claimed_by=dev required_role=developer requires_acceptance=true acceptor_agent=acc reject_count=1 task_repositories=web delivered_by=dev review_feedback=rejected%20by%20acc%20%28%231%29:%20Rejected%20commit%206a765a9.%20Failed%20criterion:%20render.js%20isOverdue%28todo%2C%20today%29%20must%20default%20today acceptance_criteria=render.js%20exports%20isOverdue%28todo%2C%20today%29:%20true%20only%20when%20todo.done%20is%20false%2C%20todo.due%20is%20a%20non-empty%20string%20and completion_continuation=active_goal note=accepted_by%3Dacc:%20Mark%20Todo%20todo_98eec0a43eb1%20complete%3B%20no%20developer%20repair%20is%20required. evidence=accepted_by%3Dacc:%20Mark%20Todo%20todo_98eec0a43eb1%20complete%3B%20no%20developer%20repair%20is%20required.%3B%20LoopX%20Turn%20validated%20completion: completed_at=2026-09-27T00:10:54-07:00 updated_at=2026-09-27T00:10:54-07:00 completion_turn_key=dispatch:58b311f01f905722e1839f37c35d0eb7 validation_command=node%20--test -->
- [x] [P2] Integrate api and web: run both suites from the workspace root and smoke the overdue flow end to end against a running todo-api
  <!-- loopx:todo todo_id=todo_ee9cf116b96c status=done claimed_by=dev required_role=developer requires_acceptance=true acceptor_agent=acc task_repositories=api%2Cweb delivered_by=dev review_feedback=accepted%20by%20acc%20but%20the%20merge%20into%20the%20target%20is%20blocked%3B%20rebase%20or%20resolve%20on%20the%20todo%20branch%20and%20deliver%20again: acceptance_criteria=From%20the%20workspace%20root%2C%20%60sh%20-c%20%27cd%20api%20%26%26%20python3%20-m%20unittest%20-q%20%26%26%20cd%20..%2Fweb%20%26%26%20node%20--test%27%60 completion_continuation=active_goal note=accepted_by%3Dacc:%20Settle%20todo_ee9cf116b96c%20as%20accepted%3B%20no%20developer%20repair%20is%20required. evidence=accepted_by%3Dacc:%20Settle%20todo_ee9cf116b96c%20as%20accepted%3B%20no%20developer%20repair%20is%20required.%3B%20LoopX%20Turn%20validated%20completion: completed_at=2026-09-27T01:39:24-07:00 updated_at=2026-09-27T01:39:24-07:00 completion_turn_key=dispatch:07500d52a262d89a7d7d072f079e3ffb validation_command=sh%20-c%20%27cd%20api%20%26%26%20python3%20-m%20unittest%20-q%20%26%26%20cd%20..%2Fweb%20%26%26%20node%20--test%27 -->
- [x] PILOT supersede check: old todo (throwaway)
  <!-- loopx:todo todo_id=todo_cd6488c157b4 status=done claimed_by=dev required_role=developer requires_acceptance=true acceptor_agent=acc task_repositories=api acceptance_criteria=throwaway completion_continuation=active_goal note=superseded reason=PILOT%20v1%20step%206%20manual%20supersede completed_at=2026-09-27T01:47:26-07:00 updated_at=2026-09-27T01:47:26-07:00 validation_command=true -->
- [x] PILOT supersede check: dependent todo (throwaway)
  <!-- loopx:todo todo_id=todo_a1c0cd76d94b status=done claimed_by=dev required_role=developer requires_acceptance=true acceptor_agent=acc task_repositories=api acceptance_criteria=throwaway completion_continuation=active_goal resume_when=todo_done:todo_2f22768c0fbf note=superseded completed_at=2026-09-27T01:52:48-07:00 updated_at=2026-09-27T01:52:48-07:00 validation_command=true -->
- [x] PILOT supersede check: replacement todo (throwaway)
  <!-- loopx:todo todo_id=todo_2f22768c0fbf status=done claimed_by=dev task_repositories=api acceptance_criteria=throwaway completion_continuation=active_goal note=superseded completed_at=2026-09-27T01:52:47-07:00 updated_at=2026-09-27T01:52:47-07:00 -->

## Next Action

- Settle todo_ee9cf116b96c as accepted; no developer repair is required.

## Recent User Feedback

- Initialized by `loopx bootstrap`.

## Progress Ledger

- Created the initial goal state and registry connection.
