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

Nothing fetches or pushes. Pushing is a user gate.
