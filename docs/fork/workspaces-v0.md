# Git multi-repo workspaces (S5)

Implements design decisions 18, 21 and 22 of [design-v0](design-v0.md).

## Goal repo list

```sh
loopx configure-goal --goal-id G \
  --repo api=/abs/api \
  --repo web=/abs/web,default_branch=develop,merge_target=task_branch \
  --execute
```

`--repo` upserts by name; `--clear-repos` removes the list. Entry shape:
`{name, path, default_branch, merge_target: main|task_branch, task_branch?}`.
Without a `repos` list, the legacy `repo` path acts as one repo named `main`
(`loopx.workspace.goal_repos`). `loopx goal create` without `--repo` sets it to
the project directory, which need not be a git repository; see
[Goals without a code repository](usage.md#goals-without-a-code-repository).
If `default_branch` is unset, it resolves to
`origin/HEAD`, then `main`, then `master`. With `merge_target=task_branch` and no
`task_branch`, the target is `loopx-task/<goal>`.

## Workspace lifecycle

```sh
loopx workspace prepare|status|merge|cleanup --goal-id G --todo-id T [--repo NAME ...] [--dry-run]
```

- **prepare** creates or reuses `<runtime_root>/goals/G/workspaces/T/<repo>` on
  the branch `loopx/G/T`, which is the same in every repo. The branch starts from the
  merge target: the task branch if one is configured (it is created from the default
  branch when missing), otherwise the default branch. Running prepare again is safe.
- **status** reports, per repo, whether the worktree is dirty, how far it is ahead of
  or behind the target, any unmerged paths, and the conflicts predicted by
  `git merge-tree`.
- **merge** is atomic across the selected repos. First it checks every repo without
  touching any working tree. It stops on a conflict, a dirty or detached todo
  worktree, or a dirty checkout of the target branch. If anything blocks, nothing is
  merged, and the result includes a `conflict_report` for the developer. If every
  check passes, each repo gets a no-ff merge commit with `LoopX-Goal` and
  `LoopX-Todo` trailers. If the target branch is checked out, that checkout is
  fast-forwarded; otherwise the ref is compare-and-swapped. If a later repo fails,
  the repos already merged are reset to their previous heads.
- **cleanup** removes only this todo's worktree paths and the `loopx/G/T` branch. It
  does so only when the branch is merged, or when `--force` is given. Unrelated
  worktrees, foreign branches at the path, and non-worktree directories are never
  touched.

Without `--repo`, the CLI actions use the todo's `task_repositories` and fall
back to every Goal repo only when the todo names none.

### Stale todo branches on release (pilot v1 gap N10)

A deferred plan todo may already have a branch: `workspace prepare` run by hand
before its dependencies merged cuts `loopx/G/T` from the merge target as it was
then. When the todo is released (`resume_ready_plan_todos`, from the dispatcher
pass or an accept verdict), each of its repos is checked
(`loopx.workspace.todo_branch_refresh`):

| state of `loopx/G/T` | action |
|---|---|
| missing | nothing (`no_branch`) |
| at the merge target | nothing (`up_to_date`) |
| an ancestor of the merge target (no commits of its own) | fast-forward to the target: `git merge --ff-only` inside its worktree, or a compare-and-swap `update-ref` when no worktree holds it |
| has commits the target lacks | never touched (`branch_has_own_commits`) |
| its worktree has uncommitted or untracked changes | never touched (`worktree_dirty`) |

The merge target is the one `prepare` uses: the task branch when
`merge_target=task_branch` and it exists, else the default branch. Nothing is
forced, reset or rebased, and a failed refresh never holds back the release.

After acceptance (S2) the accept verdict merges a todo automatically, all repos or
none, before it completes the todo. A todo is merge-eligible when its branch
`loopx/G/T` exists in its repos, even if the worktree directories were removed. A
repo that lacks the branch blocks the merge. A blocked merge returns the todo to
its developer with repo-relative conflict feedback and does not count as a
rejection. A todo's declared validation runs in this workspace too, and fails
closed (`workspace_unverified`) when the workspace is missing (see
[role-v1-protocol](role-v1-protocol.md)).

## Delivery identity (decision 29)

A Turn for a one-repo Todo runs in that repo's worktree. A Turn for a multi-repo
Todo runs from the workspace root `<runtime_root>/goals/G/workspaces/T/`, which
holds the worktrees but is not itself a git worktree. The Turn's delivery then
binds to a **todo workspace identity** instead of one repository:

| field | value |
|---|---|
| `identity_kind` / `workspace_kind` | `todo_workspace` / `todo_workspace_root` |
| `workspace_identity` | `todo-workspace:<goal>/<todo>` |
| `todo_workspace.goal_id`, `.todo_id`, `.branch` | the goal, the todo and `loopx/G/T` |
| `todo_workspace.repos[]` | per repo: `name`, `path` (relative to the root), `head_sha`, `repo_id` |

The field is optional and additive. A one-repo delivery, a legacy snapshot and
a non-role_v1 goal keep the previous single-repository shape.

In a multi-agent goal, the refresh after a Turn requires an independent
workspace. It accepts this root only when all of the following hold:

- the path is the registered root of exactly the settling goal and todo;
- every repo in it is a linked worktree of the repo the Goal declares under
  that name;
- every repo is checked out on the todo branch.

The root of another todo, a repo off the todo branch (detached or on another
branch), or a worktree of an undeclared repository is rejected, as before.

**Todos without repos.** One typed rule decides where a delivery must come
from (`peer_delivery_workspace` in
`loopx/control_plane/agents/workspace_guard.py`): refresh-state and the
`quota should-run` workspace guard both ask it. Under role_v1, a developer or
acceptor todo that names no repository (neither `task_repositories` nor
`task_repository`) has no per-todo workspace by design, so its delivery
source is the goal's project directory. Refresh then accepts the project
directory, or anything below it: a plain directory binds to the local goal
identity, a git directory to its repository identity. An independent
worktree still qualifies. A todo that names a repo, an unknown todo, a
peer_v1 goal or an agent without a role keeps the independent-worktree
requirement, and `workspace_guard_policy.peer_independent_worktree_required:
true` keeps it for every peer.
Quota for the Turn is then spent only from the same root with the same repos.
The `quota should-run` workspace guard accepts the same root. For a one-repo
todo it also accepts that repo's worktree inside the root.

**`repo_id`.** This is the canonical `remote.origin.url` identity
(`git:host/path`) when the repo has a usable origin. Without one it is
`local:` + sha256 of the realpath of `git rev-parse --git-common-dir`, so a
checkout and all of its worktrees share it. It never records a local path.
Local-only repos therefore deliver without a fake origin.

## Delivered sha and review checkouts (G12)

At delivery the tip of `loopx/G/T` in each repo is recorded in the delivery
evidence (`delivered_shas=<repo>@<sha>,...`). The accept merge passes these as
`merge(..., expected_source_shas=...)`: each repo merges exactly its delivered
sha, and a todo branch whose tip moved away from it blocks the merge with the
blocker `delivery_moved` (nothing is merged; the todo returns to its
developer).

The acceptor never runs in the todo worktree. `loopx.workspace.review_checkout`
creates a throwaway review checkout per acceptor Turn:

- `prepare_review_checkout` adds a detached worktree per delivered repo at
  `<runtime_root>/goals/G/reviews/T/<attempt>/<repo>` (`<attempt>` is the
  dispatcher run id).
- `inspect_review_checkout` reports uncommitted changes and a HEAD that is no
  longer the delivered sha.
- `remove_review_checkout` force-removes the attempt's worktrees and directory.
  It is best-effort and idempotent, and it never touches the todo worktree or
  branch.

A todo delivered before G12 has no recorded sha; its branch tips stand in for
the review checkout, and its merge is not pinned.

The delivery-identity guard above accepts a review checkout of the settling
todo in place of its workspace root: `reviews/T/<attempt>` for a multi-repo
todo (or its one repo for a one-repo todo), with every repo a linked worktree
of the declared repository detached at any commit. The identity is still the
todo workspace identity. A detached repo inside the todo's own workspace root
is still refused.

Nothing here fetches or pushes. Pushing is a user gate; see below.

## Pushing merged work (G8)

Design decisions 18 and 38. Code: `loopx/push_requests.py`.

**When the gate opens.** On every pass over a role_v1 goal, the dispatcher
opens one `push_request` user gate for the goal when both hold:

- no agent todo of the goal is `open`, `in_review`, `blocked` or `deferred`
  (all of its work is accepted and merged; a deferred todo, such as a plan
  dependent that is not released yet, is unfinished work). This is the same
  set of unfinished statuses the `goal_complete` gate waits for
  (`TODO_UNFINISHED_STATUS_VALUES`);
- a repo's merge target has commits of this goal (merge commits with the
  `LoopX-Goal: <goal>` trailer) that are not on the repo's remote.

The orchestrator can ask earlier, and the owner at any time:

```sh
loopx goal request-push --goal-id G [--agent-id ORCH] [--dry-run]
```

An explicit request does not wait for pending todos (deferred ones included)
and offers any unpushed commits on the merge target. `--agent-id` must be the
goal's orchestrator.

The gate is a system gate, like the re-login gate (decision 17): LoopX opens
it through the Todo API, and it blocks the goal's orchestrator. At most one
`push_request` gate is open per goal; further passes and requests return it.

**What the gate shows.** Per repo: the merge target branch (`loopx-task/<goal>`
or the configured task branch; the default branch when `merge_target=main`),
the remote, the commit range (`<remote tip>..<head>`, or `(new branch)..<head>`)
and at most 10 lines of `git log --oneline`. The gate text has a one-line
summary; `loopx gate show` and the dashboard gate view return the full
`push_repos` list from the gate index. Nothing touches the network to build
it: "not on the remote" means not reachable from any `refs/remotes/<remote>/*`
ref.

**Which remote.** The target branch's upstream remote (`branch.<b>.remote`),
else `origin`. A repo with neither is local-only (G3): it is listed as
skipped with a note and never pushed. So is the implicit repo `main` when the
project directory is not a git repository (`not_a_git_repo`); a declared repo
that is not a git repository stays an error. A goal whose repos are all
local-only opens no gate, and its `goal_complete` gate lists them as local
only: one set of local-only plan statuses (`PUSH_LOCAL_ONLY_STATUSES`) serves
both gates.

**Decisions** (`loopx gate resolve --decision ...`, `loopx todo complete --role
user --decision-outcome ...`, or the dashboard `gate.resolve`):

| decision | effect |
|---|---|
| approve | per repo, `git push <remote> <branch>` (never force). Each result is a `push_result` event (`ok`, or `error` with a redacted stderr tail). |
| reject | nothing is pushed; `push_declined` is recorded and the same heads are not offered again until new merges arrive |
| cancel | like reject |

**Safety.**

- Nothing is pushed unless the gate todo is closed with `approve` in the goal
  state; the push runs only after that write.
- Only the configured merge target is pushed, and only when it still is the
  configured target at approve time: a `loopx-task/` or `loopx/` branch, or
  the default branch when `merge_target=main` (this is the only way `main` is
  ever pushed).
- Exactly the approved commits: if the branch moved after the gate opened, the
  repo is not pushed (`head_moved`), and the dispatcher offers the new work in
  a new gate.
- A non-fast-forward is rejected by git and never forced.

**Failures.** If any push fails (or is refused), LoopX opens a follow-up
`push_request` gate (reason `push_failed`) whose text carries the error; its
index entry keeps the errors in `previous_errors`. Approving it retries.

**Idempotency.** The gate's outcome is stored in its index entry
(`push_outcome`); settling it again replays the outcome and pushes nothing, and
a closed gate cannot be resolved again. After a successful push the remote
tracking ref has the head, so no new gate opens.

**`on_push_command` (opt-in).** A goal entry in the registry may set
`on_push_command` (an argv list, or a string split with shell rules, never run
through a shell), for example `["gh", "pr", "create", "--fill"]`. It runs in the
repo after each successful push, with a 300s timeout; its exit status is
recorded on the `push_result` event and its output tail in the gate outcome.
It is off by default.

**State.** `goals/<G>/push/state.json` holds the declined heads and the last
push results; `goals/<G>/gates/index.json` holds the gate entries
(`kind=push_request`, `push_reason`, `push_repos`, `push_outcome`).
