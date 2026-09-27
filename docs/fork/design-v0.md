# LoopX fork design v0: role-based multi-agent orchestration

Status: agreed through a design interview on 2026-09-25. This fork no longer tracks upstream (`loopx-project/loopx`). Upstream design choices, such as flat `peer_v1` peers, may be replaced.

## Problem

Upstream LoopX has worker agents plan, decompose and self-claim work (`peer_v1`, where replan ownership is picked by hash). Worker agents are good at doing work but poor at orchestration. This fork introduces dedicated **orchestrator** and **acceptor** roles, role-based concurrent scheduling, and per-agent definitions.

## Decisions

### Roles and orchestration
1. **One orchestrator per goal.** Cross-goal coordination may be layered on later.
2. **The orchestrator runs as event-triggered headless turns**, not as a long-lived session. It reads all context from LoopX state every turn, so the state digest given to it must stay compact.
3. **A resident local dispatcher** (`loopx dispatch serve`, Python, managed by launchd) watches state events and also runs a periodic reconcile tick. It decides *who runs when* and never becomes a second state machine: all writes go through the existing CLI and kernel contracts.
4. **Every agent turn runs through the existing managed Turn pipeline** (`loopx turn run-once`). That pipeline provides the typed result, the independent validation command gate, idempotent writeback and quota spend. The dispatcher only schedules.
5. **Acceptor assignment.** The orchestrator binds an acceptor when it plans. If none is bound, the dispatcher uses the single `role=acceptor` agent. If there are several acceptors, the decision goes back to the orchestrator.
6. **Rejection loop.** The first rejection reopens the todo to the same developer with the acceptor's feedback. After 2 rejections of the same todo, it escalates to the orchestrator, which can reassign, split, change the criteria, or open a user gate.
7. **`in_review` becomes a first-class todo status.** This touches roughly 11 hard-coded status sets plus the coordination contract and generated files. No other statuses are added. Assigned, running and rejected are derived: `open` + bound agent; an active turn or lease; `open` + reject count > 0.
8. **Acceptance is required per todo** via a flag the orchestrator sets. It defaults to on for development todos and can be off for research and docs todos, which go straight to `done` once the validation command passes.
9. **Acceptance criteria have two tiers.** The goal-level acceptance contract is drafted by the orchestrator and confirmed by the user through a user gate. Per-todo criteria (text plus validation command) are written by the orchestrator. The acceptor checks both.

### The user channel
10. **User gates carry a discussion thread.** Each user reply is an event that triggers an orchestrator turn. The orchestrator either follows up on the same gate or proposes a conclusion. The user closes the gate with approve, reject or cancel. The purpose is to clarify through continued dialogue.
11. **Only the orchestrator opens user gates.** Developers and acceptors raise blockers or questions to the orchestrator. It answers what it can from the requirement docs and routes the rest to the user as gates with options and a recommendation.
12. **Plan approval.** The initial decomposition and major changes are sent to the user as a plan card through a gate. Major changes are: adding or removing a workstream, changing acceptance criteria, or an expected quota overrun. Minor adjustments are applied directly and logged: rework after rejection, splitting a todo, changing priority.
13. **Goal intake** has two entry points that share one action: CLI `loopx goal create --doc … --agents …` and a dashboard form. The requirement doc is stored and registered as an authority source, the agents and their roles are written to the registry, and the orchestrator's first turn is triggered. That turn clarifies the requirements through the gate thread and then produces a plan card.

### Agent definitions and access
14. **Agent roles are state.** The registry records `agent_id → role`. **Agent configuration lives in files**: runtime, model, reasoning effort, prompt file, permissions, `max_concurrency` and extra args.
15. **Where config files live.** They default to global `~/.codex/loopx/agents/*.yaml`, can also sit in the project at `.loopx/agents/*.yaml`, and project files override global ones. System prompts can be personalised per project.
16. **Provider config is separate** (`~/.codex/loopx/providers.yaml`). Secrets are never written to files: a provider names an env var or keychain entry, and agents reference a provider by name.
17. **Provider auth types**: `api_key`, `oauth_cli` (the CLI's own login), and `oauth_token` (for example a Claude `setup-token`). Before each launch the dispatcher runs an auth preflight. If it fails, the agent is marked unavailable and a user gate "re-login needed" is opened.

### Workspaces, concurrency and resources
18. **Git workspaces.** Each development todo gets a worktree plus branch per repo, created by the dispatcher. Conflicts go to the developer first. After acceptance, work merges into a per-goal configurable target: the main branch or a unified task branch. Pushing to a remote is a user gate.
19. **Concurrency.** The orchestrator runs strictly serially per goal. Developers and acceptors run in parallel up to their `max_concurrency`, under a global machine cap. On quota exhaustion or 429 only that provider enters cooldown with backoff. A long cooldown opens a user gate.
20. **Model.** Only `role_v1` is supported for new goals. The anti-hierarchy validators (the `profile_role` ban and the legacy `role` detector) are removed. `peer_v1` code is deleted opportunistically, when it gets in the way, rather than in one big-bang cleanup.
21. **State home is decoupled from code repos.** A goal's state can live in a central progress repo or inside a project directory. A goal declares a named `repos` list instead of a single `repo`.
22. **Multi-repo todos are supported.** A todo may target several repos (for example a frontend and backend API integration). It gets one worktree per repo and a shared branch name. Acceptance and merge are atomic across its repos, and any conflict returns the whole todo to the developer. For large features the orchestrator defaults to contract-first: an API contract todo, then parallel frontend and backend todos, then a multi-repo integration todo.
23. **Remote SSH workspaces for AOSP-scale trees.** Agents still run locally, and LoopX state stays local; cross-host state is not supported. A workspace of kind `remote-ssh` defines host, tree path and build commands. Remote execution goes through a helper (`rx`). Built images are synced back locally for testing.
24. **A leasable resource pool** covers remote source trees, build server slots and local test devices (by adb serial). The orchestrator declares the resources a todo needs. The dispatcher acquires all of them before launch or does not start the todo. Leases have TTLs and heartbeats.

### Scope
25. **MVP** is one end-to-end vertical slice covering:
    - git multi-repo todos with atomic merge;
    - three roles: orchestrator Claude Fable 5.1, developer Claude Opus 4.6, acceptor Codex gpt-5.6-sol via CPA;
    - `role_v1`, `in_review`, and reject-and-escalate;
    - the dispatcher running over `turn run-once`;
    - agent and provider config with the auth preflight;
    - gate threads and plan cards in the CLI;
    - the dashboard fixes: the web gate apply path (preview works but apply is blocked by `canonical_authority_required`), plus a role board view.
26. **Phase 2**: remote-ssh workspaces, the resource pool, and agent execution traces (traces are optional).
27. **Later**: Lark and Telegram channels.
28. **Validation.** Build small pilot projects locally and run the full loop: intake, clarification, plan approval, contract-first decomposition, delivery, at least one rejection then acceptance, merge, answering a gate in the web UI, and the role board. Add kernel unit tests for role-aware selection, `in_review` transitions, the reject count and escalation, and atomic multi-repo merge.

## Decisions after the E2E pilot (2026-09-26)
29. Delivery identity is a todo workspace identity (goal, todo, branch, per-repo name/path/head/repo_id); repo_id falls back to a local git-common-dir digest when there is no origin. Approved by the user.
30. Per-todo acceptance criteria live in a dedicated orchestrator-owned `acceptance_criteria` todo field, shown to developer and acceptor every Turn; rework instructions go to `review_feedback`. Approved by the user.
31. role_v1 goals do not raise the upstream vision-checkpoint or no-follow-up replan obligations; planning review is the orchestrator's job every Turn, and developers/acceptors escalate through orchestrator todos. Approved by the user.
32. Under role_v1 the developer/acceptor Turn lane is per todo; one agent runs up to max_concurrency todos of a goal in parallel, the orchestrator stays serial per goal. Approved by the user.
33. `in_review` under canonical hard_lease stays unsupported for now (default soft_claim covers MVP). Decided by the user.
35. The acceptor only reviews: it runs unsandboxed on a throwaway detached checkout of the delivered commit; merge uses the recorded delivered sha. Approved by the user.
36. Acceptor verdicts are accept / reject (feedback naming failed criteria required) / blocked; blocked opens a system user_gate (retry, accept manually, return to developer, cancel) and does not count as a rejection. Approved by the user.
37. A dependency that requires acceptance releases its dependents only when accepted and merged; replacing or splitting a todo uses `todo supersede`, which rewires dependents and never counts as done. Approved by the user.
39. role_v1 goals do not raise the upstream self-reported-wait / projection-repair demand for an idle orchestrator; stuck work is detected by the dispatcher (gate replies, escalations, replan obligations). Approved by the user. The periodic-review replan (`periodic_review_due`), a sibling found by the survey, is not derived for role_v1 either; stall replans from run history are.
40. After initial planning, a change to a todo's acceptance criteria takes effect only through a user-approved plan card; the acceptor does not review that todo while the card is pending. Approved by the user.

## Kernel seams (from code exploration)
- **Registry roster:** `loopx/agent_registry.py`, `loopx/configure_goal.py`, `loopx/cli_commands/registry_admin.py`. The runtime model enum is in `loopx/control_plane/agents/runtime_model.py`; the anti-hierarchy rules are in `control_plane/agents/profile.py` and `legacy_migration.py`.
- **Selection choke point:** `loopx/control_plane/todos/quota_selection.ts:121-183`, with its Python packer `quota_selection.py`.
- **Replan routing** is currently hash-based: `goals/goal_frontier/__init__.py:~296` and `task_orchestration_admission.py:~112`.
- **Todo contract:** `control_plane/coordination/coordination_state_contract_v0.json` plus the generator `scripts/generate_coordination_state_contract.py`. The status sets are hard-coded in about 11 places.
- **Turn pipeline:** `cli_commands/turn.py` and `control_plane/turn_driver/{executor,codex_cli}.py`. The `claude-code` host can be planned but not run. Codex has no generic `-c` passthrough, so a `--codex-config` option is needed for CPA.
- **Web gate apply:** `chat_actions.py:1376-1384`. The CLI gate path is `cli_commands/todo.py:557` → `todos.py:1554`.
