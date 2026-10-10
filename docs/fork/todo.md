# Fork backlog

This page lists the fork's known open problems and planned follow-ups. Each
item links to its tracking issue or pull request when there is one. Items move
off this page when they are resolved. The [changelog](../../CHANGELOG.md)
records what has shipped.

Status as of 2026-10-09 (`main` at `22ea3b05a`, latest release
[v2.1.0](https://github.com/foolzzz/loopx/releases/tag/v2.1.0)).

## Blocking the next release

- **[#143](https://github.com/foolzzz/loopx/issues/143) (P0): the default
  Python CI fails on `main`.** The semantic inventory slack-disclosure test
  assumes the `conflicting_values` slack is listed first. After the cleanup
  batch, other legitimate slack counters can come first, so the assertion
  fails although the report discloses every slack correctly. Fix the test
  contract so that it does not depend on order, then re-run the failing test,
  the architecture modules and the affected default CI on the new head. v2.2.0
  waits on this fix.

## Open pull requests

- **[#142](https://github.com/foolzzz/loopx/pull/142): bounded Codex CLI
  routing plans and independent profiles.** This is the first of three slices
  that move `loopx-codex-provider-routing` from the retired Codex App to Codex
  CLI with CPA. It binds the route at launch and leaves online routing to CPA.
  Review approved it and the offline acceptance passed. The delivery is
  partial: acceptance against a real CPA instance is held until the deployment
  prerequisites below are settled. The pull request is waiting for an owner
  merge decision.
- **[#116](https://github.com/foolzzz/loopx/pull/116) /
  [#114](https://github.com/foolzzz/loopx/issues/114): installer candidate
  doctor diagnostics.** The pull request keeps the failed doctor checks
  visible when a release candidate is rejected. Under high machine load, the
  commands probe timed out three times and the root cause is not proven. Run
  it again on a lightly loaded machine before merging.

## Open issues

- **[#104](https://github.com/foolzzz/loopx/issues/104) (P2): concurrent
  local release promotion smoke is intermittent.** Three runs ended
  fail/pass/fail. The last failure did not observe the installer PID in the
  empty-lock window. It is still unclear whether this comes from slow startup,
  from an observation race in the smoke, or from the product.

## Planned work

- **Provider routing, slices 2 and 3.** Slice 2 pins and recovers the route
  for governed `exec` Turns. Slice 3 covers Chat and app-server, plus the
  Dashboard and Lark companion work. Both follow #142. The decided
  prerequisite is to reuse the existing CPA service. Still open: the routing
  target (a single native subscription, or a failover pool), the real-request
  budget, and the heterogeneous provider fallback, which stays off by default.
- **Real-model end-to-end rerun.** The last full end-to-end run with a real
  model predates the App removal and the cleanup batch. Run a disposable goal
  through the dispatcher with the `codex-cli` host.
- **Update Notes wording (P2).** The opening of
  `docs/update-notes/README.md` still promises a two-week publishing cadence,
  but the scheduled draft generator has been retired. Describe that cadence as
  historical and point to the current explicit preparation flow.

## Known limitations

- The CLI closes a typed gate before its settlement records the chosen
  option. If the process dies between the two steps, the option is not
  recorded, and a retry settles with the option that the retry passes.
- When the dashboard cannot read a typed gate's option list, it disables that
  gate's decisions. Decide it with `loopx gate resolve --option ...` instead.
- If `run-once` is killed while its host process keeps running, the goal's
  single Turn slot can be released early.
- Closing a goal does not remove the developer worktrees. Use
  `loopx workspace cleanup` for each Todo.
- Concurrency in gate settlement has been tested with threads only, not with
  multiple processes.
- Validation gaps carried by the v2.1.0 release: the no-clone check only
  covers checkouts with a built Chat bundle; the CI keeps a set of skipped
  Python and TypeScript tests; real model hosts, benchmark runs and a full
  public smoke sweep were not executed.

## Risks to re-check

These were found during earlier reviews and have not been re-verified against
the current `main`. Confirm each one before fixing it, and open an issue when
it still reproduces.

- The Todo storage codec may compress the acceptance criteria and commands
  of an applied Todo.
- `source_only` writes and state migration take their locks in opposite
  orders. A conflict can wait up to the 5-second lock timeout.
- The path redaction pattern may mangle `https://` URLs, and host stderr
  redaction only covers a fixed set of roots.
- Settlement atomicity:
  - a gate decision and its follow-up effects are not one transaction;
  - an acceptance merges before its final check, with no mutual exclusion
    between concurrent accept and reject;
  - escalating after a second rejection takes two writes.
- Push approval is not bound to a remote URL and an exact commit set.
- With no verification command and only a soft budget, spending has no hard
  upper limit.
- Dispatcher operation:
  - the dispatcher is a single point of failure, and it identifies itself
    weakly after a restart;
  - a wake-up can be lost;
  - goals are scheduled in a fixed order, so later goals can starve;
  - observability is limited.
