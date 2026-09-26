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
(`loopx.workspace.goal_repos`). If `default_branch` is unset, it resolves to
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

Nothing fetches or pushes. Pushing is a user gate.
