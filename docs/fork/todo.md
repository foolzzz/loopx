# Fork backlog

This page lists the fork's known open problems and planned follow-ups. Each
item links to its tracking issue or pull request when there is one. Items move
off this page when they are resolved. The [changelog](../../CHANGELOG.md)
records what has shipped.

Status as of 2026-10-09 (`main` at `634231c4f`, latest release
[v2.1.0](https://github.com/foolzzz/loopx/releases/tag/v2.1.0)).

## Picking this up

- Start with [#143](https://github.com/foolzzz/loopx/issues/143). Until it
  is fixed, the default Python CI on `main` is red, and every other change is
  harder to validate.
- Read the repository [`AGENTS.md`](../../AGENTS.md) for the contribution
  rules: dedicated worktrees, DCO sign-off, pull requests, the first-screen
  review gate and the public/private boundary. Read the
  [usage guide](usage.md) for how the fork is installed and run.
- Frozen data must stay byte-for-byte unchanged: the JEV sentinel recordings
  and other provenance-backed fixtures. Two broad cleanups in this batch
  rewrote such data by accident and had to be restored. Exclude these paths
  before any repository-wide search and replace.

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

## Dependency vulnerability alerts

GitHub Dependabot reports 36 open alerts on `main` (3 critical, 18 high,
15 moderate). They all come from pinned lockfiles and requirement files. None
has been triaged yet for reachability. For each manifest, upgrade to the first
patched version and re-run the tests that cover it.

| Manifest | Packages (highest severity) |
| --- | --- |
| `packages/dsh-loopx-plugin/pnpm-lock.yaml` | `proxy-addr` (critical); `fast-uri`, `@modelcontextprotocol/sdk`, `js-yaml`, `sharp`, `source-map-js` (high); `hono`, `ip-address` (moderate) |
| `apps/presentation/dashboard/package-lock.json` | `seroval` (critical); `source-map-js` (high) |
| `apps/presentation/site/package-lock.json` | `source-map-js` (high) |
| `tests/requirements-stage2c-linux-py311.txt` | `PyJWT` (critical, high and moderate; one moderate advisory has no patched version yet) |
| `package-lock.json` | `brace-expansion` (moderate) |

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
- **`loopx update apply` and the upstream installer default to upstream.**
  Their defaults target `loopx-project/loopx` at `--ref stable`, not this
  fork. Running them replaces a fork install with the upstream product. The
  [usage guide](usage.md) warns against it, but the defaults themselves still
  point upstream. Point them at the fork, or make a fork install refuse an
  upstream source.
- **Typing debt.** CI runs strict `mypy` only on an allowlist of modules in
  `pyproject.toml`. Running strict `mypy` over all of `loopx/` reports 4,058
  errors in 517 files. Widen the allowlist module by module.
- **Load-sensitive tests.** Several tests and smokes wait on fixed timing
  windows and fail when the machine is heavily loaded:
  - installer candidate doctor probes ([#114](https://github.com/foolzzz/loopx/issues/114));
  - the release promotion lock windows ([#104](https://github.com/foolzzz/loopx/issues/104));
  - a few subprocess barrier and probe timeouts that pass when re-run on
    their own.

  Separate real failures from timing noise before trusting a red run, and
  replace fixed windows with explicit readiness signals where a test owns the
  wait.
- **Release process is manual.** The release-artifact and scheduled
  update-note workflows were retired. A release is now: a version-bump PR
  (`pyproject.toml`, `loopx/__init__.py`, man page, `CHANGELOG.md`), an
  annotated tag on the merge commit, and a GitHub release whose body passes
  `examples/release/release-readiness-doc-smoke.py`.

## Known limitations

- The CLI closes a typed gate before its settlement records the chosen
  option. If the process dies between the two steps, the option is not
  recorded, and a retry settles with the option that the retry passes.
- When the dashboard cannot read a typed gate's option list, it disables that
  gate's decisions. Decide it with `loopx gate resolve --option ...` instead.
- A goal without a code repository runs its developer and acceptor Turns
  directly in the project directory, one Turn at a time. If `run-once` is
  killed while its host process keeps running, that single slot can be
  released early.
- Closing a goal does not remove the developer worktrees. Use
  `loopx workspace cleanup` for each Todo.
- Concurrency in gate settlement has been tested with threads only, not with
  multiple processes.
- Upgrading from 2.0.0 is manual. Move `~/.codex/loopx` to `~/.loopx` and
  each project's `.codex/goals` to `.loopx/goals` yourself; there is no
  migration. Retired App scheduler state files and the `loopx-apply-rrule`
  link are left in place and can be deleted by hand. See the
  [changelog](../../CHANGELOG.md).
- Validation gaps:
  - the no-clone check only covers checkouts with a built Chat bundle;
  - CI keeps a set of skipped Python and TypeScript tests;
  - the `claude-code` host was only unit-tested; no real Turn ran on it;
  - native Windows and PowerShell paths have not been run;
  - benchmark runs did not execute;
  - with the Full Public Smokes workflow retired, nothing runs a full public
    smoke sweep automatically.

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
