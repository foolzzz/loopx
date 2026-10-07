# Testing And Quality / 测试与质量体系

LoopX coordinates long-running agents, so a change can be locally correct yet
still alter which work an agent selects, whether it asks a user, or whether a
host keeps running. The quality system therefore tests one shipped behavior at
several distances. Fast deterministic checks protect pull requests; broader
and more expensive checks run only when their signal justifies the cost.

LoopX 协调长程 agent。一个局部正确的改动，仍可能改变 agent 选择哪项工作、是否
向用户提问，或 host 是否继续运行。因此质量体系从不同距离验证同一套已交付行为：
快速、确定性的检查保护每个 PR；更广、更昂贵的检查只在信号值得成本时运行。

## Quality Layers / 质量分层

| Layer / 层 | What it proves / 证明什么 | Normal cadence / 常规频率 |
| --- | --- | --- |
| Unit and contract tests / 单元与合同测试 | Pure rules, schemas, transition tables, invalid-state rejection / 纯规则、schema、状态转换和非法状态拒绝 | Every relevant PR in `python-tests.yml` / 相关 PR 必跑 |
| Durable public smokes / 稳定公开 smoke | Shipped CLI and cross-module behavior through public-safe fixtures / CLI 与跨模块交付行为 | Focused locally; full-public by explicit CLI run / 本地聚焦；显式 CLI 全量运行 |
| Catalog-informed canary / Catalog 驱动 canary | The smallest risk-based slice spanning every changed public surface / 覆盖所有变更面的最小风险切片 | Before sensitive merge or release / 敏感合并与发布前 |
| CLI output budgets / CLI 输出预算 | Agent-facing output stays bounded and base-to-head growth is visible / 输出有界且能发现相对增长 | Relevant PR CI and premerge / 相关 PR CI 与 premerge |
| Public-safe decision replay / 公开安全决策回放 | Reviewed source-state invariants replay through the real quota-to-scheduler path / 经审阅的源状态不变量重放真实 quota-to-scheduler 链路 | Regression and control-plane changes / 回归与控制面变更 |
| Model-behavior qualification / 模型行为验证 | A real model correctly interprets the actual default packet and safety contract / 真实模型能正确理解当前默认载荷与安全合同 | Low-frequency local/manual shadow gate / 低频本地或手动影子门 |
| Release outcome baseline / 发布结果基线 | Stable release and candidate outcomes are comparable under matched semantics / 稳定版与候选版在匹配语义下可比较 | Release qualification or scheduled observation / 发布验证或周期观察 |

These layers are complementary. A model pass cannot override a deterministic
contract failure, and a large smoke sweep cannot replace a focused regression
that names the broken rule.

这些层次互补：模型通过不能覆盖确定性合同失败；大规模 smoke 也不能代替明确指出
错误规则的聚焦回归测试。

High-risk shipped surfaces are registered in the machine-audited quality
surface catalog. Each row names an independent semantic oracle and classifies
every layer as `covered`, `not_applicable` with a reason, or `deferred` with an
owner. This avoids both silent gaps and meaningless requirements such as using
a model to judge deterministic scheduler precedence.

高风险交付面需要进入可机器审计的质量 surface catalog。每一行都要指明独立语义
oracle，并把每一层明确分类为 `covered`、有理由的 `not_applicable`，或有 owner 的
`deferred`。这样既不会静默漏测，也不会强迫模型去判断确定性的 scheduler 优先级。

```bash
loopx canary quality-audit
```

Run it from a source checkout to validate that referenced product, test, and
documentation paths still exist; packaged installs retain the classification
audit but report that repository-reference validation is unavailable.

在源码 checkout 中运行时，命令还会验证引用的产品、测试和文档路径仍然存在；打包
安装仍可审计分类，但会明确报告仓库引用校验不可用。

The audit fails on an unclassified high-risk canary profile, an oracle that
reuses product source as expected truth, a missing deterministic minimum, or
an unexplained exception. Valid deferred rows remain visible as backlog gaps
without pretending that the repository is fully qualified.

如果新增高风险 canary profile 却未分类、oracle 直接复用产品实现作为期望值、缺少
确定性最小门禁，或例外没有理由，审计会失败。合法的 deferred 行仍作为 backlog
缺口显式展示，不会伪装成仓库已经完全就绪。

Tests first judge whether the state and rule are correct, then whether the
implementation conforms. Expected outcomes come from an independently reviewed
invariant, never the implementation under test or its current output.
Characterization fixtures document legacy behavior but do not authorize it;
contradictions require rule repair and negative or mutation coverage, not a
refreshed golden.

测试先判断状态和规则是否正确，再验证实现是否符合。预期结果必须来自独立审阅的
不变量，不能由被测实现或当前输出生成。Characterization fixture 只记录历史
行为，不授予其正确性；发现矛盾时应修复规则并增加反例或 mutation 覆盖，不得刷新
golden 来让测试通过。

## Pull-Request Baseline / PR 基线

### Required Merge Check / 必需合并检查

`python-tests.yml` publishes `merge-gate` for every pull request. Code,
workflow, policy and unknown paths require the existing `pytest` aggregate
(including TypeScript checks and all four Python shards), Stage 2C correctness
aggregate, and Windows tests to succeed. Failed, cancelled, missing or
unexpectedly skipped results cannot pass the gate. The client-only exception
below retains common checks and substitutes packaged Dashboard qualification
for unrelated backend jobs.

For a change limited to allowlisted root Markdown or `docs/**/*.md`, the
classifier explicitly skips the expensive core jobs and the aggregate checks
that they were skipped. Runtime prompts, executable documentation, deleted
code and code renamed into docs are not documentation-only exemptions. A
missing Git base or failed classification fails closed.

`Sign-off` and `merge-gate` form the required-check contract, bound to GitHub
Actions. The [live ruleset](https://github.com/huangruiteng/loopx/rules/18121976)
is authoritative for activation. The lead maintainer alone retains the
existing bypass exception; record the exact head, reason, validation and known
failures whenever using it. A bypass does not turn failed tests into a pass.

每个 PR 都会收到 `merge-gate` 结果。代码、工作流、治理规则和未知路径必须通过
原有核心测试；失败、取消、缺失或意外跳过均不能通过。仅白名单根目录 Markdown
或 `docs/**/*.md` 的修改可显式跳过昂贵测试；运行时 prompt、可执行文档、代码删除
及代码移入文档均不享受豁免。实际启用状态以在线规则为准，使用 owner bypass
必须留下版本、原因、验证和已知失败的记录。

To validate or change the classifier locally:

```bash
python -m unittest discover -s scripts/ci -p test_review_gate.py
python scripts/ci/review_gate.py classify --base origin/main --head HEAD
```

Changes to the classifier or workflow need both code-path and documentation-only
qualification. Keep required check names stable and never require a
workflow-level path-filtered check that cannot report on every PR.

The [job exemption policy](ci-impact-selection.md) additionally permits pure
Dashboard-client changes to skip backend Python/Windows and Stage2c, while
requiring common checks and the real packaged Dashboard build/browser smoke.
Mixed, prompt, dependency and unknown changes stay full. Full Python runs four
complete shards and combines all four coverage files. The `ci:full` label forces
full qualification; main stays full. No selected-only report impersonates full
coverage. 新的前端豁免由目标分支已审阅的策略控制；CI 自身变更仍全量验证。

PRs opened before activation may need a branch update to produce the new
required check; an old green suite alone does not supply a missing aggregate.

Browser waits must target the intended surface: a visible pending-message label
and an accessibility live region may contain the same words. Use a scoped or
exact locator and retain the bounded completion assertion; do not suppress a
strict-mode ambiguity with a retry or a longer timeout. 浏览器等待应区分消息提示与
无障碍播报，不能把定位歧义误报成恢复超时，也不能通过删掉播报来让测试通过。

### Refactor Real-Path Gate / 重构真实路径门

The PR review capability's `observable_semantics` evidence gate applies to
behavior-bearing changes, including extractions and backend migrations; it
does not infer equivalence from a refactor title. Reviewers inventory legacy
caller branches at an immutable baseline and compare the same synthetic
inputs at the exact head through the public entrypoint. Include successful
and rejected paths, full diagnostics/remediation, validation precedence,
supplied/omitted/empty/clear arguments, persisted readback, ownership and
receipts, plus replay/concurrency where relevant. A provider suite whose
implementations share the new rule is not a before/after compatibility test.

重构评审必须区分“相同判定”和“相同可观察语义”：同样拒绝但丢失对象状态或修复
提示仍是回归；新增认领前提可能阻断旧实现允许的未认领文案修正；`--note` 被 parser
接受或出现在成功响应中，也不能证明它经过 dispatch 后落盘并能独立回读。先从旧调用
方及公开契约列出合法／非法分支，再运行基线与精确 head 的对照，不从新实现生成期望。

Show regression sensitivity: the focused case must fail on the historical
defect or a deliberate dropped-field/detail or stronger-precondition mutation,
then pass on the fix. Preserve intended changes as explicit justified deltas;
do not freeze a known baseline bug or normalize away meaningful differences.
Re-review the full inventory after a correction, not only the previous finding.
Missing comparison evidence blocks an equivalence-based approval. This is a
reviewer-executed requirement projected in the packet, not a machine proof
that the comparison ran. The packet tests protect this requirement's delivery;
runtime regressions must still test actual behavior.

保留“旧缺陷／语义 mutation 失败、修复后通过”的证据；有意变更需单列理由与验证，
不能盲目追求字节一致。复审重查完整兼容清单，不沿着上一条发现自动走向批准。缺少
对照证据就不能宣称等价；packet 测试只证明要求被投影，不证明 agent 已执行或产品无缺陷。

The evidence must be executable, not merely descriptive. Record a replayable
command or bounded script invocation for baseline, exact head, and the
sensitivity case, together with the real affected backend, immutable fixture
fingerprint, exit status, and normalized observation fingerprint. Baseline and
head use the same harness and fixture unless an intentional delta is declared.
The mutation run must traverse the same public entrypoint and make an
independent oracle fail; helper-only unit coverage or conformance among
providers that all share the candidate rule cannot satisfy this gate.

证据必须可执行，不只是文字描述。基线、精确 head 和敏感性用例都要记录可重放命令
或有界脚本调用，以及真实受影响 backend、不可变 fixture 指纹、退出状态和归一化观测
指纹。除非声明并证明有意差异，baseline 与 head 必须使用同一 harness 和 fixture。
Mutation 运行必须经过同一 public entrypoint，并让独立 oracle 失败；只测 helper，或让共享
候选规则的多个 provider 互相 conformance，不能通过该门禁。

Refactors must exercise the affected production entrypoint and real backend
before delivery. Unit tests, mocks, and in-memory conformance remain useful,
but cannot replace that proof. For changes affecting PostgreSQL authority,
configure `LOOPX_TEST_POSTGRES_URL` to an isolated disposable server and run
`npm run test:postgresql-authority-store`; a skipped suite is an evidence gap,
not a pass. Record the exact commit, backend version, tested behavior, and
failures or limits. The `PostgreSQL Integration` workflow runs that ladder row
and the service admission suite against a disposable server, so the path stays
qualified in CI as well as locally. If no safe real environment is available,
hold delivery.

重构交付前必须验证受影响的真实生产入口和真实后端。单测、mock 与内存 conformance
不能替代这项证据。影响 PostgreSQL authority 时，配置指向隔离临时实例的
`LOOPX_TEST_POSTGRES_URL`，运行 `npm run test:postgresql-authority-store`；跳过不算
通过。记录精确 commit、后端版本、验证行为与失败或局限；`PostgreSQL Integration`
workflow 会在临时实例上运行该 ladder row 与 service admission 套件，因此这条路径在
CI 与本地都会保持验证。没有安全的真实环境则暂停交付。

When one semantic owner fronts multiple authority providers, use a three-arm
refactor comparison: the immutable legacy baseline, the file provider, and a
real PostgreSQL provider. All three arms must start from the same
production-complex snapshot and execute the same public operations. Compare
the two provider heads exactly, then compare the baseline semantically after
the declared compatibility projection. The allowed archive normalization is
narrow: omit provider-retained `archive_state=archive` records from the legacy
hot view, omit their historical leases from that hot view, and ignore absolute
imported `index` values only after separately proving identical per-role
relative order. Provider provenance may also differ. No domain field, archive
selection, relative order, active lease, or non-target record may be normalized.
File/PostgreSQL agreement alone is not compatibility evidence because both
providers execute the same new rule. Verify that every non-target record and
the source snapshot are unchanged.

The source snapshot's `lease_inventory` is byte-level audit evidence for every
legacy lease file observed under the source locks. It is intentionally broader
than canonical `projection.leases`, which contains only live edges whose Todo is
present in the current canonical graph. Historical orphan lease files remain in
the source inventory and never enter the canonical head.

当同一个 semantic owner 服务多个 authority provider 时，重构对照必须包含三臂：
不可变 legacy baseline、file provider、真实 PostgreSQL provider。三臂从同一份生产
复杂度快照出发，执行相同 public operation；两个 provider head 要精确相等，baseline
按显式 compatibility projection 比较。归档场景只允许三项窄归一化：legacy hot view
不包含 provider 保留的 `archive_state=archive` 记录、不包含这些记录的历史 lease；并且
只有先单独证明每个 role 的相对顺序完全一致，才可忽略导入 `index` 的绝对值。provider
provenance 也可不同。domain 字段、归档选择、相对顺序、active lease 和非目标记录均
不得归一化。File/PostgreSQL 一致不能单独证明兼容，因为它们执行的是同一套新规则。
还要验证所有非目标记录与源快照保持不变。

源快照中的 `lease_inventory` 是在 source lock 下观察到的全部 legacy lease 文件的
字节级审计证据；它有意比 canonical `projection.leases` 更宽。后者只包含当前 canonical
Todo 图中仍有对应 Todo 的 live edge。历史 orphan lease 文件继续留在 source inventory，
但绝不进入 canonical head。

The archive rehearsal is executable and emits only bounded counts and digest
prefixes. It never prints raw projections, Todo identifiers, source paths, or
the PostgreSQL URL. The URL must name a disposable isolated server:

```bash
LOOPX_TEST_POSTGRES_URL="$DISPOSABLE_POSTGRES_URL" \
python examples/control_plane/authority-three-arm-rehearsal.py \
  --registry "$REGISTRY_PATH" \
  --goal-id "$GOAL_ID" \
  --execute-isolated-postgresql
```

归档三臂演练可直接执行，且只输出有界计数和 digest 前缀；不输出 raw projection、
Todo 标识、源路径或 PostgreSQL URL。URL 必须指向一次性隔离服务。命令中的显式
`--execute-isolated-postgresql` 只是安全确认，不会授权连接共享或生产数据库。

Keep tests separate from active state: use a disposable database/tenant and
runtime directory, with synthetic fixtures or an owner-authorized read-only
snapshot. Never run the integration suite against a shared or production
database: it creates roles and injects schema-level failure triggers. Never
promote an active goal, change its provider, or mutate its registry, writer
fence, Todos, or leases for a test. Keep private snapshots and raw outputs out
of Git and public reviews; compare source fingerprints before and after an
owner-authorized snapshot rehearsal. A concurrent source change is reported,
not overwritten or restored by the test. Stop the temporary server afterward.

测试使用独立临时数据库／tenant 和 runtime 目录，输入为合成 fixture 或经 owner 授权
的只读快照。集成测试会创建角色并注入 schema 级失败 trigger，禁止连接共享或生产库。
不为测试晋升正在运行的 goal、切换 provider，或修改其 registry、writer fence、Todo、
lease。私有快照和原始输出不得进入 Git 或公开 review；快照演练前后比较源指纹。
发现并发源变更只报告，不擅自覆盖或恢复。测试后停止临时数据库。

Keep a deterministic, public-safe production-scale fixture beside the focused
cases. Its envelope should cover realistic role/status distributions,
multi-agent claims, user gates and standing decisions, current and retired
leases, successor links, validation markers, archival pressure, and enough
history to exercise ordering and capacity-sensitive paths. Generate content
from public-safe seeds rather than copying production text or identifiers, and
run the same fixture through every provider conformance suite. This fixture is
a durable regression layer; it complements, but never substitutes for, the
read-only three-arm rehearsal against current production-complex state.

在聚焦用例之外，长期保留确定性、public-safe 的生产规模 fixture。其 envelope 应覆盖
真实的 role/status 分布、多 Agent claim、User gate 与 standing decision、当前与已退役
lease、successor link、validation marker、归档压力，以及足以触发顺序和容量敏感路径的
历史规模。内容必须由公开安全的 seed 生成，不复制生产文本或标识；同一 fixture 要进入
所有 provider conformance suite。它是持久回归层，只补充、不替代针对当前生产复杂状态
的只读三臂演练。

### Production-scale fixture stewardship / 生产规模 fixture 维护契约

For decision-owner migrations, inventory command producers as well as state
fields. A large real-state snapshot does not exercise commands synthesized only
inside an executor: run unchanged production caller chains, including reclaim,
replay and stale-writer rejection, before claiming caller closure. Persistent
public grants and clock-authorized ephemeral executor grants are separate
contracts; neither a broad allowlist nor agreement across providers proves parity.

迁移决策 owner 时，既要盘点状态字段，也要盘点命令生产者。真实大快照不会自动覆盖
executor 内部生成的 reclaim 等命令；必须运行未改动的完整调用链，包括接管、重放和
旧执行者拒绝，再声明调用方已闭合。持久公开 grant 与时钟授权的临时 executor grant
是不同合同，不能用扩大 allowlist 或 provider 间一致替代行为对齐。

Treat `tests/fixtures/control_plane/coordination_production_scale_v0.json`
and its generator as a shared acceptance input for both the TypeScript
control-plane migration and shared-goal-authority RFCs. A pull request that
changes provider-neutral fields, coordination semantics, or capacity and
retention assumptions must carry a fixture impact declaration: extend the
fixture and an independently derived assertion through every affected provider
arm, or state why the existing dimensions fully cover the change. Storage-only
provider work may use the unchanged fixture, but still runs the affected arm.
Runtime routing, promotion, or compatibility work also runs the read-only
three-arm rehearsal; the fixture never upgrades synthetic agreement into live
promotion evidence.

Fixture improvements are welcome when they encode an accepted RFC invariant or
a reproduced public regression that the current envelope misses. Keep each
addition deterministic, bounded, and public-safe; derive expected behavior
from the invariant rather than the generator output. Add at least one negative
or mutation-style assertion that would fail if the new dimension were ignored,
reuse the same envelope and generator across providers, and do not weaken or
remove an existing dimension without a reviewed compatibility reason. Never
copy production text, identifiers, paths, logs, credentials, or private
snapshots into the fixture. Report the fixture schema, semantic dimension,
provider arms, and intentional deltas in the PR validation evidence.

将 `tests/fixtures/control_plane/coordination_production_scale_v0.json` 及其
generator 视为 TypeScript control-plane migration 与 shared-goal-authority 两份 RFC
共用的验收输入。修改 provider-neutral field、coordination 语义，或容量／保留假设的 PR，
必须附带 fixture 影响声明：扩展 fixture 与独立推导的断言，并让它通过所有受影响的
provider arm；或者说明现有维度为何已经完整覆盖该改动。仅修改 provider 物理存储时可以
复用未变化的 fixture，但仍要运行受影响的 arm。涉及 runtime routing、promotion 或
compatibility 的工作还要执行只读三臂演练；fixture 绝不能把合成数据一致性升级为真实
promotion 证据。

欢迎开发者把已接受的 RFC invariant 或已复现、但当前 envelope 尚未覆盖的公共回归沉淀
进 fixture。每次增强都要保持确定性、有界且 public-safe；expected behavior 必须从
invariant 独立推导，不能从 generator 当前输出反推。至少增加一个在忽略新维度时会失败的
negative 或 mutation-style 断言，并让各 provider 复用同一 envelope 和 generator；没有
经 review 的兼容理由，不得削弱或删除已有维度。禁止复制生产文本、标识、路径、日志、
凭据或私有快照。PR 验证证据需报告 fixture schema、语义维度、provider arms 与有意差异。

### Local Validation Environment / 本地验证环境

Run from the repository or dedicated worktree root with `uv`. The project's
`requires-python` declares Python `>=3.11`; it does not require an executable
named `python3.11`. `uv` selects a compatible interpreter, creates `.venv`, and
installs the checkout with the selected extras. Interpreter downloads depend on
uv's download settings and network access. A system `python3` may be too old,
and a global `loopx` may resolve to a different installed source tree.

在仓库或独立 worktree 根目录使用 `uv`。`pyproject.toml` 要求 Python `>=3.11`，
无需依赖名为 `python3.11` 的命令。uv 选择兼容解释器，在 `.venv` 中安装当前源码
与测试依赖；能否自动下载 Python 取决于下载配置与网络。系统 `python3` 可能过旧，
全局 `loopx` 也可能指向另一个已安装版本。

```bash
uv sync --extra test
uv run --extra test python -m ruff check tests loopx/canary loopx/control_plane loopx/domain_packs loopx/presentation
uv run --extra test python -m mypy
uv run --extra test python examples/control_plane/cli-output-budget-regression-smoke.py
uv run --extra test python -m pytest -q
uv run --extra test loopx canary premerge --from-git-diff
# For a fork whose PR base is upstream/main, use this instead:
uv run --extra test loopx canary premerge --from-git-diff --git-diff-base upstream/main
# Validate semantics; optionally inspect the full-tree inventory without writing it:
uv run --extra test loopx canary smoke-suite --script semantic-vocabulary-drift-smoke.py
uv run python scripts/generate_semantic_inventory.py
git diff --check
```

The semantic inventory is computed from the full tracked tree, not committed.
Use `--output .local/semantic-inventory.json` only when an exported report is useful;
`--output <path> --check` checks that explicit report without repairing it.
词表、owner 与预算继续入库并受检查；结构清单按需计算，无须为普通 PR 补生成文件。

Confirm the interpreter and imported checkout when diagnosing a mismatch:

```bash
uv run python -c "import sys, loopx; print(sys.executable); print(loopx.__file__)"
```

Node-based TypeScript tests and browser smokes that launch Python use
`scripts/test-python.mjs`. It honors an explicit `LOOPX_TEST_PYTHON` (then the
existing `LOOPX_PYTHON_BIN`/`LOOPX_PYTHON` overrides), otherwise reuses the
source launcher's selection on POSIX or discovers a compatible interpreter on
Windows. It prefers the worktree environment, checks Python `>=3.11`, and
fails with a setup hint instead of falling back to an incompatible system
`python3`. A source-level regression test rejects new bare-`python3`
subprocess/fallback patterns in test and browser-smoke entry points. After
`uv sync --extra test`, `npm run test:control-plane` needs no manual Python
environment variable; `LOOPX_TEST_PYTHON=/path/to/python` is an explicit
override when a separate compatible environment is intentional.

会启动 Python 的 Node/TypeScript 测试和浏览器 smoke 统一使用
`scripts/test-python.mjs`：显式覆盖优先，否则优先当前 worktree 环境，校验
Python `>=3.11`；不会静默退回不兼容的系统 `python3`。回归测试会拦截测试入口
重新引入裸 `python3` 子进程或默认值。

Canary executes Python checks with the interpreter that launched LoopX
(`sys.executable`). Its displayed `python3` command is not a second interpreter
selection. Keep subprocesses on `sys.executable`; use `uv run` at the developer
entrypoint. Avoid `uvx loopx` or `uv run --no-project` when validating this
checkout, and change into the intended worktree before running Git-based checks.
An activated compatible environment remains a supported alternative: install
with `python -m pip install -e ".[test]"`, then use that environment's Python
and LoopX commands directly.

Canary 使用启动 LoopX 的 `sys.executable` 执行 Python 检查，显示的 `python3`
不是重新选择解释器。子进程继续复用 `sys.executable`，只在开发入口使用 `uv run`。
检查当前源码时不要改用 `uvx loopx` 或 `uv run --no-project`；Git diff 检查前先进入
目标 worktree。已有兼容虚拟环境也可用 `python -m pip install -e ".[test]"` 安装源码，
随后直接使用该环境的命令。

The repository does not currently track `.python-version` or `uv.lock`. `uv`
creates a local lockfile during resolution; keep that generated file out of
unrelated PRs. Introducing a shared lock or interpreter pin is a separate
repository policy change. Do not claim identical environments from
`requires-python` alone, or use `--locked` before a reviewed lockfile exists.
CI keeps its explicit Python versions and pinned/hash-checked installation
paths. Historical validation receipts keep the commands that actually ran.

当前仓库未跟踪 `.python-version` 或 `uv.lock`。uv 解析依赖时生成的本地锁文件不要
混入无关 PR；共享锁文件与解释器版本固定应单独评审。最低版本要求不等于环境完全
可复现，没有已评审锁文件时也不使用 `--locked`。CI 保留显式 Python 版本与固定依赖／
哈希校验的安装路径，历史验证记录保留实际执行过的命令。

`.github/workflows/python-tests.yml` runs this fast lane for relevant Python
pull requests. It intentionally excludes provider-backed evaluation and the
full smoke catalog, so ordinary iteration does not depend on credentials,
network latency, provider availability, or a two-hour matrix.

`.github/workflows/python-tests.yml` 会在相关 Python PR 上运行这条快速通道。
它刻意不包含真实模型调用和 full smoke catalog，因此普通迭代不依赖凭证、网络
时延、模型服务可用性或两小时级测试矩阵。

The Linux suite uses four hosted runners with two xdist workers each.
`pytest-split` partitions the complete collection using `least_duration`;
without a timing file, tests have equal weight and alternate between shards.
Lint, type checks, and the CLI budget run separately. The required `pytest`
check rejects failed/skipped shards and missing coverage artifacts, then uses
`coverage combine` to enforce the existing 19.6% floor on the union, not on
individual shards. Relative coverage paths make reports portable across runners.
The reusable Sonar workflow consumes that same run's XML and never reruns
pytest or reads cross-run artifacts. Missing Sonar tokens still skip analysis
successfully; test jobs receive no Sonar secret. The trigger is the union of
the former Python and Sonar paths. Client-only PRs use the exemption above;
Sonar-configuration changes remain full, including on forks without a token.

Linux 全套测试分到四台 hosted runner，每台保留两个 xdist worker。`pytest-split`
按完整 collection 分片；没有历史耗时时，等权测试交替分配。lint、类型检查和 CLI
预算独立执行。必需的 `pytest` 汇总检查会拒绝失败／跳过的分片和缺失的 coverage，
合并后再执行原有 19.6% 门槛；不要求单个分片达到全套覆盖率。coverage 使用相对路径，
Sonar 只复用同一次 run 的 XML，不重复测试、不跨 run 取产物。缺少 token 仍成功跳过
Sonar，测试 job 不接收 Sonar secret。触发范围取原有两套 workflow 的并集；纯前端
PR 使用前述豁免，Sonar 配置变更仍全量运行，包括没有 token 的 fork。

Reproduce one shard locally with `uv run --extra test python -m pytest -q -n 2 --splits 4 --group 1
--splitting-algorithm least_duration --cov=loopx`. Omit the split arguments to
run the complete suite locally. 全量本地测试仍省略分片参数即可。

## Smokes And Canary / Smoke 与 Canary

A durable smoke should protect shipped behavior, a reusable contract, a
public/private boundary, or a regression that previously stranded automation.
It should not preserve dated research prose or raw execution evidence.

Use [What counts as a good smoke](good-smokes.md) for the contributor checklist,
semantic-oracle examples, public-safe fixture rules, and consolidation process.

Durable smoke 应保护已交付行为、可复用合同、公开/私有边界，或曾让自动化卡死的
回归；不应固化某次研究文案或原始执行证据。

贡献者检查项、语义 oracle 示例、公开安全 fixture 规则和合并流程见
[什么是好的 Smoke](good-smokes.md)。

The repository hygiene smoke is the thin baseline for LoopX's own public
checkout: it asserts required tracked files, runs the canonical public/private
boundary scan, and ratchets the release timeline to every published version
tag.

仓库卫生 smoke 是 LoopX 自身公共 checkout 的薄基线：断言必需跟踪文件存在，
执行规范的公开/私有边界扫描，并把 release 时间线收紧到每个已发布版本 tag。

```bash
python3 examples/repository-hygiene-smoke.py
```

Run one focused smoke while developing, then let the canary planner select the
smallest cross-surface set from the Git diff:

```bash
python -m pytest tests/control_plane/test_public_safe_decision_replay.py -q
loopx canary premerge --from-git-diff
```

`premerge` resolves the caller's Git root for diff hygiene, changed-Python
compilation, and public-boundary scanning, while LoopX's installed canary
catalog continues to run from its own trusted release root. This also supports
running the command from another repository or one of its subdirectories.

`premerge` 会把调用方 Git 根目录用于 diff 卫生检查、变更 Python 编译和公开边界扫描；
LoopX 已安装的 canary catalog 仍从自身可信 release root 运行。因此该命令也可从其他
仓库或其子目录执行。

The complete public sweep remains explicit and bounded:

```bash
mkdir -p smoke-results
loopx --format json canary smoke-suite --suite full-public --jobs 4 --timeout-seconds 120 \
  > smoke-results/full-public.json
```

The full-public GitHub workflow has been retired. Run the sweep explicitly
when needed; no scheduled GitHub check or workflow artifact is required.

full-public GitHub 工作流已退役。需要时显式运行全量检查；不再要求定时 GitHub
检查或工作流产物。

### Smoke Fleet Health / Smoke 集群健康

The health command can merge receipts from explicit full-public runs into one
compact report. It separates four coverage groups instead of treating every
smoke as an equal PR requirement: the explicit PR-fast smoke, catalog-selected
canaries, the full-public sweep, and high-risk release profiles. The legacy
`daily_full_public` and `daily_only` labels classify coverage, not a scheduled
workflow. It aggregates duration, failures, timeouts, current-inventory coverage, and targeted profile
ownership without copying stdout/stderr tails or repository-local paths.

健康命令可把显式 full-public 运行的回执聚合为紧凑报告。报告区分四组覆盖，
而不是把每个 smoke 都变成 PR 必跑项：显式 PR 快速 smoke、catalog 选择的 canary、
full-public 全量，以及高风险 release profile。保留的 `daily_full_public` 和
`daily_only` 标签表示覆盖分类，不代表存在定时工作流。它会统计耗时、失败、超时、
当前清单覆盖率和定向 profile owner，同时不会复制 stdout/stderr tail 或仓库本地路径。

```bash
loopx canary smoke-health --receipt smoke-results
```

The default output is a bounded review summary. Use `--include-inventory` only
for the explicit diagnostic cold path. Exact-content duplicates and direct
nested execution are review candidates; similar names or shared profile
membership are not enough to declare two semantic contracts equivalent. The
health audit never deletes or migrates a smoke automatically. A daily-only row
without a targeted profile is an ownership backlog signal, not proof that the
test is valueless.

默认输出是有界的审阅摘要；只有显式诊断冷路径才使用 `--include-inventory`。内容完全
相同或直接嵌套执行只会成为审阅候选；名称相似、共同属于某个 profile，不足以证明两个
语义合同等价。健康审计不会自动删除或迁移 smoke。只有 daily 全量覆盖、没有定向
profile 的条目表示 owner 待澄清，并不等于该测试没有价值。

## Agent-Facing Output Budgets / Agent 输出预算

The interface budget gate measures stable command scenarios and compares the
candidate checkout with its base. It catches accidental payload growth,
duplicated diagnostics, and hot-path fields that silently return after a
refactor. Budgets protect useful, bounded decisions; historical ceilings are
not optimization targets. Explain added or removed semantic fields and use the
decision process below when changing a budget. Default agent-facing projection
changes remain subject to the existing owner review.

接口预算门会测量稳定命令场景，并比较 candidate 与 base，捕获意外膨胀、重复诊断
以及重构后悄悄回到热路径的字段。预算保护有用且有界的决策，历史上限不是优化目标。
解释新增或删除的语义字段，预算调整按下述流程判断；默认 agent-facing 投影变化
仍遵循既有的 owner review。

The full diagnostic packet remains an explicit drill-down surface. Moving a
field off the default path is acceptable only when the default still tells the
agent what to do and how to request the omitted detail.

完整诊断包保留为显式 drill-down。只有默认路径仍能告诉 agent 下一步做什么、以及
如何请求被省略细节时，字段才能移出默认热路径。

### Budget Failure Decisions

Classify the limit by its owning contract before deciding how to repair a
failure. This applies to output size/structure and latency regression budgets;
it does not grant execution quota, spending, or provider authority.

先根据所属合同判断上限的性质，再选择修复方式。这适用于输出尺寸、结构和延迟
回归预算，不授予执行配额、费用额度或 provider 权限。

| Limit / 上限 | Decision boundary / 决策边界 |
| --- | --- |
| Hard external or authorized limit / 外部或授权硬上限 | Respect the transport, storage, resource or owner constraint; a test edit cannot raise it. / 遵守传输、存储、资源或 owner 约束，改测试不能扩容。 |
| Regression budget / 回归预算 | A measured guard against unexplained growth; preserving, reducing or increasing it needs consumer and cost evidence. / 用测量防止无解释增长；保持、压缩或扩容都依据消费者价值与成本。 |
| Presentation cap / 展示上限 | Bound returned detail, not source truth; preserve selection, totals, completeness and drill-down. / 限制返回细节而非源事实；保留选择、总数、完整性与下钻路径。 |

1. **Measure the same contract.** Record base/head revisions, workload, metric
   and measurement boundary. Compact JSON characters, UTF-8 bytes, nested keys,
   emitted stdout and tokens are different metrics. For latency, retain the
   sample window, workload and distribution; acknowledge noise. Keep the
   failing scenario and original result; do not shrink fixture populations,
   scan roots or sampling depth to obtain a pass.
2. **Inspect information value and redundancy.** Name the current consumer and
   decision each changed field supports. Remove derivable or unused copies when
   the consumer contract permits it. Similar rows in different lanes may serve
   different consumers; deduplicating them needs caller migration/parity, not
   just equal JSON. Prefer bounded summaries and reachable cold paths for
   detail. Do not delete identity, completeness, safety or settlement semantics,
   shorten names solely to pass, or build a reference framework for tiny savings.
3. **Choose and disclose the tradeoff.** Compare compaction, retaining the
   ceiling, and a justified increase; a combination is valid. For an increase,
   explain the remaining useful cost, old/new ceiling, measured headroom and
   expected variation or scale. No universal headroom percentage is required.
   Update the owning contract and test expectation together, rerun the original
   scenario and affected semantic/scale checks, and report both the original
   failure and new result in existing PR validation and review evidence. A
   budget-only change need not force unrelated code cleanup. Frozen experiment
   or promotion thresholds stay fixed for that result; revised thresholds belong
   to a new qualification, never a relabeled historical pass.

1. **同口径测量。** 记录 base/head、负载、指标和测量边界。紧凑 JSON 字符、UTF-8
   字节、嵌套键数、真实 stdout 和 token 不可互换。延迟要保留样本窗口、负载和
   分布，并承认噪声。保留失败场景与原结果，不缩小 fixture、扫描范围或采样深度。
2. **分析信息价值与真实冗余。** 说明变化字段服务哪个消费者、哪个决策。合同允许时
   删除可推导或无人使用的副本；不同 lane 中相同的数据可能服务不同消费者，去重
   需要调用方迁移和语义等价验证。详情优先使用有界摘要和可达冷路径。不能删身份、
   完整性、安全或结算语义，不能只为过线缩字段名，也不为微小收益制造引用框架。
3. **选择并披露取舍。** 比较压缩、保持上限、合理扩容，也可组合使用。扩容需说明
   保留信息的价值与成本、新旧上限、实测余量和预期波动或规模，不规定统一余量比例。
   合同和测试同步修改，重跑原场景及受影响的语义/规模检查，在既有 PR 验证和评审
   证据中同时保留原失败与新结果。纯预算调整不必捆绑无关清理。已冻结的实验或
   promotion 阈值不能追溯放宽；新阈值属于新一轮验证，不能改写历史结论。

A base/head differential must remain able to measure a syntactically and
semantically valid base that already exceeds its own historical ceiling;
otherwise the gate deadlocks the repair before observing the candidate. The
base-only probe may skip absolute size assertions while retaining parse,
required-key, anchor, and semantic differential checks. The candidate always
runs the current absolute budgets. Measurement-only mode is never a candidate
override or merge bypass.

当 base 已超过自身历史上限但输出仍可解析且语义完整时，base/head differential 必须
仍能采集它；否则门禁会在观察修复候选之前形成死锁。仅 base 的 probe 可跳过绝对尺寸
断言，但必须保留解析、必需字段、锚点和语义差异检查；candidate 始终执行当前绝对
预算。measurement-only 不能用于 candidate，也不是合并旁路。

The PR-review packet's `semantic_alignment` rule consumes this evidence through
the existing `validation_matrix` and `observable_semantics` rows. It does not
add a separate budget receipt or approval gate. The result checker verifies
evidence structure and verdict consistency; the reviewer still judges whether
the measurements and tradeoff are sound.

PR-review 的 `semantic_alignment` 通过既有 `validation_matrix` 和
`observable_semantics` 使用这些证据，不增加独立预算回执或审批门。结果校验器检查
证据结构和结论一致性；测量是否可信、取舍是否合理仍由评审判断。

## Decision Replay And Issue #2191 / 决策回放与 #2191

Issue #2191 is the reference pattern for a cross-layer control-plane
regression. The final `interaction_contract` is scheduler authority; raw
`should_run` and lower-level compatibility fields may not override it. A
non-blocking `user_action` also cannot satisfy an agent todo's blocking
`required_decision_scopes`.

Issue #2191 是跨层控制面回归的参考模式：最终 `interaction_contract` 是调度权威，
原始 `should_run` 和底层兼容字段不能越权；非阻塞 `user_action` 也不能满足 agent
todo 的阻塞 `required_decision_scopes`。

The regression is protected at four deterministic levels:

1. a data-driven scheduler decision table checks human gate, active work,
   repair, mapped no-op, and successor-replan cases;
2. todo-scope tests distinguish a compatible blocking gate from a notice, an
   unrelated agent gate, and a dangling scope;
3. the real quota builder must turn a scope collision into bounded
   control-plane self-repair while disabling normal delivery;
4. a public-safe fixture stores source todo facts plus an independently reviewed
   invariant and expected outcome; the replay smoke and catalog canary rerun the
   real quota-to-scheduler path without raw state, logs, prompts, trajectories,
   or local paths.

该回归由四层确定性测试保护：数据驱动调度决策表；todo scope 所有权测试；真实 quota
builder 的 fail-closed 集成测试；以及从源 todo 事实重新执行真实链路、再与独立审阅
不变量比对的公开安全回放与 catalog canary。

The expected replay outcome must never be generated by the implementation under
test. Reducer shape checks remain useful for redaction and compatibility, but
they are separate from the semantic oracle. Metamorphic cases also assert that
adding an unrelated agent gate or mutating lower-level compatibility fields
cannot change the current agent's final decision.

回放期望值绝不能由被测实现生成。Reducer shape 测试仍可验证脱敏和兼容性，但必须
与语义 oracle 分开；变形测试还会验证，新增其他 agent 的无关 gate 或修改底层兼容
字段，都不能改变当前 agent 的最终决策。

The narrow mutation check deliberately flips lower-level signals and proves
they cannot preempt a final human gate. This is stronger than asserting one
expected JSON snapshot because it exercises the precedence rule directly.

窄 mutation 检查会主动翻转底层信号，证明它们不能抢占最终 human gate。这比只比对
一份 JSON snapshot 更强，因为它直接验证优先级规则。

## Release-only native Goal regression / 仅发布前的原生 Goal 回归

`scripts/qualify-native-goal-release.py` exercises the real Codex CLI app-server
Goal lifecycle on a disposable ledger project with two dependent Todos. It
reuses the shipped native Goal transport and current prompt, then checks an
independent acceptance oracle, completed Todos, unique bound spends, durable
writeback readback, and terminal no-follow-up quota. This is not a benchmark
score or evidence of universal model reliability.

The Codex release arm requests the shipped bootstrap, not an injected private
work recipe. Deterministic regressions execute the saved CLI loader, change its
registry inputs, and prove fresh loading, non-recursion, preserved explicit
policy and removed-agent rejection. Claude's stdio regression loads
`host_prompt` through the real MCP transport and verifies the same bound Goal.
These tests are free of model calls; passing them is not a live model pass.

Upgrade regression uses real temporary SQLite/TOML stores, a second connection,
writer-lock contention, injected mirror failure, journal recovery and stale or
custom-input rejection. New-runtime reconciliation is exercised through the
real CLI, while package installation is substituted in that focused test.
Running-App deployment additionally needs a selected owner-authorized canary
and delayed readback; synthetic SQLite tests alone do not qualify App caches.
The output differential permits a bounded one-time transition to the exact
static-safety marker, not permanent growth allowances or relaxed quota budgets.

```bash
# No model invocation, no token cost; explicit skipped result, exit 0.
python3 scripts/qualify-native-goal-release.py
# Release operator opt-in only; explicit isolated API profile (Responses API).
# Supply LOOPX_CODEX_QUALIFICATION_API_KEY securely in this process, plus:
export LOOPX_CODEX_QUALIFICATION_MODEL='<selected-model>'
export LOOPX_CODEX_QUALIFICATION_BASE_URL='https://example.com/v1'
python3 scripts/qualify-native-goal-release.py --release-live
```

Do not add the live command to default pytest, PR CI, per-diff canaries, or
ordinary developer iteration. The deterministic runner-policy tests may run
there; they never opt into real model execution. Missing CLI, native Goals or
the explicit Codex API profile returns `skipped` and exit 0, not a claimed live pass.
Once qualification is attempted, failed acceptance, incomplete settlement,
blocked/unfinished Goals and deadline expiry fail with exit 1. The default
deadline is 1,200 seconds; this is a wall-clock ceiling, not a token budget.

仅 release 前显式开启，避免默认消耗开发者 token。CI/本机环境不支持时跳过且不阻塞，
但保留 `skipped` 标记；真实执行后失败不能冒充环境跳过。使用操作者显式选择的 API
模型、地址与密钥，不导入日常 Codex 配置、登录或会话，不修改活跃 Goal/automation。
任务、registry、runtime 与 Git worktree
在一次性目录内；沙箱允许该目录及本地 TS worker 所需的网络能力，
这不是网络隔离，任务不授权外部操作。回归脚本不采集或上传原始对话/工具日志，
公开结果仅包含状态、计数和错误类别；Codex 会话仅留在一次性隔离目录内。
Codex runner 从允许列表创建环境并隔离 HOME、配置和缓存；不透传其他 token、
认证 socket、shell 启动变量或原始 ARK_API_KEY。Codex 工具 shell 从空环境注入必要
运行变量，不继承 host API key。

### Claude Code and release coverage / Claude Code 与发布覆盖

Before calling a changed host surface release-qualified, distinguish:

| Boundary | Required evidence |
| --- | --- |
| Work and terminal closeout | Final candidate, actual host, independent artifact checks, completed Todos and terminal quota; code delivery alone is insufficient. |
| Idempotency and failure | Real committed lifecycle/writeback/spend followed by lost-response injection and same-intent retries; one final spend. Failed declared validation must not complete or spend. |
| Authority and transport | Actual MCP initialization/tool invocation and mismatched-agent rejection; existing claim/lease and validation suites remain required. |
| Host lifecycle | Native scheduler wakeup/cancellation/resume on supported versions, reported separately from headless execution. |
| Upgrade and isolation | Exact managed-wrapper recognition, preview/apply revision checks, preserved scheduler state, explicit skips, no default model calls or leaked test processes. |

普通 CI 只跑确定性规则、真实 CLI/MCP 和故障注入；模型执行仍仅 release 前显式启用。
环境缺失可 skip 且退出成功，但最终版本没有完整的真实 host 结果时，不得写成
“产品级发布验证通过”。单次成功也不是模型可靠性或长程调度 soak 的证明。

The real delivery regression covers ordinary Todo acceptance before internal
writeback/spend, existing-successor linking, and receipt-backed terminal closure.
Its task specification describes only the deliverable; the external oracle also
checks LoopX accounting. Passing non-delivery fixtures does not qualify delivery.
The same delivery class must pass failed-validation rejection and committed
response-loss recovery without duplicate spending or premature terminal closure.
Do not relabel delivery work or weaken the independent oracle to pass a host test.

### MCP vision authoring and recovery / MCP vision 写入与恢复

The MCP guard projects `interaction_contract.mcp_channel`. For admitted normal
Todo delivery, it replaces the raw CLI writeback/spend instructions with
`complete_task` ownership; those are alternate transports, not two obligations.
Replan-only and blocked lanes preserve their live CLI actions and binding.
Vision field and total limits come from the same TS validator, not copied prompt
constants. Quota admission, permission and workspace facts are unchanged.

MCP 的普通 Todo 交付不再同时要求模型执行 CLI 记账和 MCP 结算两套流程；独立 replan
仍使用动态 CLI 契约。vision 字段与预算直接来自 TS 校验器，不要求模型猜格式或翻测试。

`complete_task` accepts either `agent_vision` (the existing bounded
`goal_vision_replan_contract_v0` JSON packet) or `vision_unchanged_reason`.
An unchanged decision needs a persisted valid baseline. The TypeScript host
plan forwards that authored decision to its ordinary writeback; v1 requests
fail closed against old runtimes instead of silently dropping the fields.
Syntax and vision-budget preflight reuse the TS validator before lifecycle writes;
baseline-dependent checks still run at writeback. Oversized authoring is a
correctable input failure, not a terminal Goal failure. If an older/interrupted
host already completed the Todo but failed writeback, retry `complete_task` with
the same completion intent and a corrected uncommitted vision. Checkpoint-only
recovery is not a substitute for unfinished settlement.

If a previously completed MCP Todo omitted its decision, call
`review_task_vision(todo_id, agent_id)` with that same Todo to read its current
decision basis. Judge the returned basis, then submit `agent_vision=...` or
`vision_unchanged_reason=...` together with `read_context_id=...`. A stale or
replaced receipt requires another read and a new judgment; a lost response
requires the exact same receipt and decision, without reading again.
It uses the original host Turn and the same writeback command constructor,
delegating to the existing typed checkpoint recovery. It neither repeats Todo
completion nor spends again. Exact replay is idempotent; a conflicting committed
decision or a later superseding vision is rejected. It does not change Next
Action or erase other work, gates, or permissions. A genuinely new replan follows
the current interaction contract under a fresh admitted binding, not an edit to
an already committed decision. Claude Todo-less replan now projects that identity
re-entry before any refresh/spend instructions; ordinary MCP Todo delivery is
unchanged.

Todo acceptance, settled accounting, checkpoint satisfaction and Goal termination
are separate facts. `vision_closed` closes a stage and still requires a successor
vision for an active Goal. `no_followup` is an authored scoped closure assertion,
not a substitute for evidence; remaining acceptance gaps or gates still prevent
terminal quota. Kernel validation does not independently prove arbitrary prose
true, so behavior qualification must also inspect the delivered artifacts.

MCP 可随完成操作携带 vision 判断，也可用 `review_task_vision` 在原 Turn 补齐遗漏：
先只传 Todo 和 Agent 读取依据，重新判断后携带 `read_context_id` 与判断提交。
凭据过期或被替换时重读重判；响应丢失时原样重试凭据和判断，不重新读取。
复用 TS 的既有恢复规则，不新增结算引擎、不重扣额度；已提交的判断不能偷偷改写。
格式和预算预检在 Todo 完成前拒绝非法输入；若旧宿主已部分完成，则修正未提交的
vision 并重试原 `complete_task`，不能用仅补 checkpoint 的操作替代未完成结算。
“checkpoint 满足”不等于“Goal 完成”，`vision_closed` 只结束阶段，真实缺口仍须规划。
外层任务只描述业务验收，LoopX 协议由宿主内层指令和工具承接。

## Exact Release Commit Gate / 精确发布 Commit 门

The final release gate does not rerun tests through a second orchestration
framework. It aggregates compact receipts from the existing lanes and proves
that they all qualify the same clean source identity:

最终发布门不会通过第二套编排框架重新运行测试。它聚合现有测试通道的紧凑回执，并
证明它们都对应同一个干净源码身份：

```bash
loopx canary release-qualification \
  --manifest-json release-qualification.json \
  --repo-root .
```

`exact_release_commit_qualification_manifest_v0` repeats `git_commit`, Git
tree id, clean-tree status, package version, and version tag in every check
receipt. The CLI also derives those fields from `--repo-root`. A missing,
failed, skipped, dirty, rebased, or differently versioned receipt fails closed;
results from an earlier commit cannot qualify a later tag.

`exact_release_commit_qualification_manifest_v0` 会在每个检查回执中重复
`git_commit`、Git tree id、干净工作树状态、包版本与版本 tag；CLI 还会从
`--repo-root` 独立读取这些字段。缺失、失败、跳过、脏工作树、rebase 漂移或版本不一致
都会 fail closed；旧 commit 的结果不能给新 tag 背书。

| Receipt / 回执 | Semantic minimum / 语义下限 |
| --- | --- |
| `pytest` | At least one pass and zero failures / 至少一项通过且零失败 |
| `ruff`, `mypy` | Zero violations or type errors / 零 lint 或类型错误 |
| `risk_canary` | Selected checks passed with no unresolved manual hold / 已选检查通过且没有未解决人工 hold |
| `full_public` | Health receipt from explicit full-public runs is ready with no failure or timeout; no GitHub workflow artifact is required / 显式 full-public 运行的健康回执 ready 且无失败或超时；不要求 GitHub 工作流产物 |
| `install_upgrade_host` | Install, upgrade, and host routes all passed / 安装、升级与 host 路径均通过 |
| `public_boundary` | Zero public/private violations / 零公开私有边界违规 |

Alongside repeated source identity and status, each receipt keeps only its
result schema, result digest, completion time, and bounded counters. Result
schema names are fixed per lane, and public-boundary evidence must show that at
least one path was scanned. Commands, stdout/stderr, prompts, packets, model responses,
credentials, and local paths stay outside the manifest. The command is a
read-only qualification reducer: it runs no checks, calls no model, moves no
ref, creates no tag, and publishes nothing. A ready receipt still requires the
owner's release decision.

每份回执只保留结果 schema、结果 digest、完成时间和有界计数。命令、stdout/stderr、
prompt、packet、模型响应、凭证与本地路径都不得进入 manifest。该命令只是只读资格
reducer：不运行测试、不调用模型、不移动 ref、不创建 tag、也不发布。即使回执 ready，
仍需 owner 作出发布决定。

## Benchmark Research Evidence / Benchmark 研究证据

Deterministic and model-behavior gates qualify a control-plane contract; they
do not prove that a release improves long-running outcomes. Outcome claims use
a small stable-release-versus-candidate manifest with matched task semantics,
runner protocol, model, reasoning level, timeout, and repetitions. A mismatch
or incomplete arm fails closed and cannot automatically promote a release.

确定性测试和模型行为测试验证控制面合同，但不能证明发布提升了长程结果。结果声明
必须使用小规模 stable-release-vs-candidate manifest，并匹配任务语义、runner、模型、
reasoning、timeout 与重复次数。任一不匹配或不完整都 fail closed，且不能自动发布。

Current benchmark studies follow the
[research RFC](../architecture/rfcs/long-horizon-harness-benchmark-research-program-v0.md)
and live in the repository-level [benchmark workspace](https://github.com/huangruiteng/loopx/blob/main/benchmark/README.md).
Legacy release reducers are archived and no longer form an active CLI or release
qualification surface.

当前 benchmark 研究遵循上述 RFC，并在仓库级 `benchmark/` 工作区中沉淀。旧 release
reducer 已归档，不再属于 active CLI 或 release qualification surface。

## Risk-Based Review / 按风险审阅

| Change / 变更 | Minimum gate / 最小门禁 |
| --- | --- |
| Docs or copy only / 仅文档 | Link and boundary check, focused doc smoke, `git diff --check` |
| Pure rule or schema / 纯规则或 schema | Unit table plus focused smoke |
| Scheduler, quota, todo, gate, onboarding / 调度与接入 | Unit table, real integration, replay, catalog canary, owner review |
| Default agent-facing output / 默认 agent 输出 | Above plus CLI base/head budget and semantic field ledger |
| Provider-backed behavior / 真实模型行为 | Deterministic gates first, then repeated one-arm local shadow receipts |
| Release promotion or outcome claim / 发布晋级 | Relevant canary, release checks, matched outcome baseline, explicit owner decision |

Sensitive behavior changes should remain easy to review: state the authority
rule, list fields added or removed, name the exact deterministic and model
behaviors checked, report skips, and keep automatic promotion disabled unless
the release contract explicitly permits it.

敏感行为变更应便于审阅：说明权威规则，逐项列出字段变化，写清验证过的确定性与模型
行为，报告跳过项；除非发布合同明确允许，否则保持自动晋级关闭。

## Evidence Boundary / 证据边界

Before opening a pull request, scan only candidate public paths:

```bash
loopx check \
  --scan-path CONTRIBUTING.md \
  --scan-path docs/development/ \
  --scan-path loopx/control_plane/ \
  --scan-path tests/ \
  --scan-path examples/
```

Never commit credentials, private state, raw benchmark material, model
responses, local absolute paths, or generated run logs. Prefer compact fixture
ids, reason codes, digests, and public-safe semantic projections.

禁止提交凭证、私有状态、原始 benchmark 材料、模型响应、本地绝对路径或生成日志。
优先保留紧凑 fixture id、reason code、digest 和公开安全语义投影。
