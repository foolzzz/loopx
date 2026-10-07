# Codex CLI 多订阅与多 Provider 路由 Runbook

本包的当前维护面是无密 CLI 计划与显式手动 profile 出口。CPA 是唯一在线路由层，
Codex CLI 拥有 session，operator 拥有明确授权的部署与凭据。本包不接管模型请求、
账号刷新、LoopX Turn、Chat、Dashboard 或 Lark。历史 App 运行结果不能证明 CLI 资格。

## 当前交付边界

| 维护面 | 当前结果 | 证据边界 |
| --- | --- | --- |
| 公共 extension v1 | 闭合 request/response，无密 catalog、CLI launch declaration 和观察契约 | 不产生文件、网络、凭据或模型请求副作用 |
| CLI plan | 固定 route/revision/deployment，声明 model/effort/tier/能力 | `declaration_only`，不是已应用的 launch binding |
| 手动 CLI profile | CLI 0.160.0 独立 `<name>.config.toml`，明确目标白名单 | 不写旧 `[profiles]`、全局 config、auth 或 session |
| 离线 readback | 真实 CLI 配置解析与隔离 app-server `model/list` | 不证明 entitlement、模型请求或实际 slot |
| 在线 CPA 验收 | **held / partial** | 等待部署、锁定版本与真实请求预算；不借用 App 历史资格 |
| governed Turn / Chat / Dashboard / Lark | 未接入 | 没有自动消费 plan 的共享用户入口 |

详见 [CONTRACT.md](CONTRACT.md) 的字段与 [OPERATOR.md](OPERATOR.md) 的准确命令。
这里的空权限是应用契约，**不是 OS sandbox**；相同系统用户的插件隔离需要另行建立。
managed extension 不应获得 CPA key、management key、OAuth 或兼容 provider 凭据环境。

## 激活与读回

先在同一 Python 环境安装包并显式注册 extension，再运行公开合成例：

```sh
python3 -m pip install packages/loopx-codex-provider-routing
loopx extension install \
  --manifest packages/loopx-codex-provider-routing/extension.toml \
  --execute --format json
loopx extension doctor loopx-codex-provider-routing --execute --format json
loopx extension run loopx-codex-provider-routing \
  --input-json packages/loopx-codex-provider-routing/examples/cli-plan.json \
  --execute --format json
```

读回 `result.schema_version`、`qualification` 和 `online_qualification`。
`ok=true` 只表示输入合法，qualification 还要单独看 checks。纯观察不能认证 caller 的陈述。
opaque route/deployment ref 不是 endpoint/path/env 解析权限；实际映射仅由可信 operator 配置提供。

operator 默认 dry-run；每次执行必须显式 `--execute`。先生成 catalog/profile，再显式安装到
已配置的独立 home。用户逐命令 `codex --profile NAME` opt-in；不会自动修改 LoopX 宿主。
配置切换只供下一次显式启动使用，不能把旧活动 session 显示成已采用新 route。
独立 profile 先用 `codex --profile NAME debug prompt-input` 做解析，
不保留 prompt 输出。CLI 0.160.0 的 `app-server` 不接受 `--profile`；operator 把同一 profile
白名单字段转换成受限 argv `-c` overrides，再用 app-server 的 `model/list` 与 `config/read`
核对。两步均在隔离 HOME/CODEX_HOME 完成，不发送真实模型请求，也不代表 Chat 已接入。
公开 synthetic metadata 与固定公共 instruction 占位符只用于 CLI 0.160.0 解析形状；正式
运行前 owner 还需确定锁定的可信 metadata 和官方 instruction 来源，不声明模型行为资格。

## 路由准入与恢复

catalog 可以描述一个 bounded account ring，Prefer 改首选入口，affinity 只重排仍然合格的
成员。fallback tail 不是环成员，不得回跳。实际线上 eligibility 必须在每 attempt 用完整
history 校验 image、effective priority/Fast、custom tools、reasoning/compaction/context。
本版本 CLI plan 只接纳过滤后原生候选；异构 fallback 资格未完成，不能靠 metadata 放行。
不准剥离图片、降级 custom tool、删除历史或替换小模型来使请求通过。

CPA 的 commit barrier 是透明切换上限：首个已向下游提交的生成内容或 tool item 之前才可
切换；之后返回 typed failure，通过原 session/effect owner 恢复。外层 CLI 非零退出不证明
可安全换账号、换 provider 或重放工具。同 session resume 和 LoopX spend 幂等不等于工具
exactly-once；效果无法确认时 hold，不新建路由专属副作用账本。

| 观察契约 | 必须证明 | 不足以证明 |
| --- | --- | --- |
| `qualify_quota_recovery` | reset 晚于 cooldown 来源，旧 cooldown 失效，同账号 bounded reprobe | 另一账号成功或 cache reset 成功 |
| `qualify_outage_recovery` | 新 recovery signal、bounded probe、degraded affinity 清理/能力复核 | 文本 fallback HTTP 200 |
| `qualify_tool_transport` | 原 item 类型保留且 dispatch 完成 | function-only provider 自称兼容 custom tool |
| `qualify_stream_recovery` | deadline 大于已观察静默间隔、retries 不增加、小事件增量透传、upstream terminal、原 session/home text+tool 连续性 | 新 session、synthetic terminal、单纯 HTTP 200 |

这些 API 只检查无内容 caller observation，不读 raw SSE/history、不 reset cooldown、不改
配置、不 resume session，也不证明底层实现已经在线验收。fixture 时限不是默认重试建议；
不沿用 App 的 30/30 或旧 CPA retry/cooldown 数字。先量测同版本同负载，再固定总预算。

## 验收矩阵

| 场景 | 决定性验收 | 状态边界 |
| --- | --- | --- |
| schema/runtime 一致 | 合法请求和输出共同通过，unknown/raw/prompt/private ref 被两边拒绝 | 离线 |
| 功能关闭 | 未安装或未选 profile 时，无新启动字段、配置改写、网络探测 | 离线 |
| CLI 配置 | 隔离 HOME，真实 CLI 0.160.0 解析独立 profile，受限 -c 的 model/list 与 config/read 核对 model/provider/catalog | 离线，不发模型请求 |
| operator | 合成 state 的 dry-run、白名单、symlink/路径拒绝、snapshot integrity、rollback | 离线，不碰真实 HOME |
| 原生模型请求 | 锁定 CPA 部署的 text、tool 与原 session 连续性，实际 account/route readback | held：部署和预算 |
| 原生账号 failover | pre-commit A→B，一次完整回答/工具结果；post-commit 禁止改道和重放 | held：部署和预算 |
| image/Fast/custom tool | 每 attempt 独立准入，不合格 tail typed fail-closed | held：部署和预算 |
| provider-bound history | foreign reasoning/compaction、tool 因果链及 incompatible history 明确拒绝 | held：部署和预算 |
| 恢复与性能 | stale cooldown/affinity、response loss、取消、deadline 与实际 attempt/TTFT | held：部署和预算 |

运行具体离线命令见 [README.md](README.md#validation)。结果保留 passed、failed、untested/held
区别；不把全绿、空查询或模型目录可见当作真实请求资格。真正上线前，operator 应按 deployment
锁定 binary digest、source/version、route revision、metadata/catalog digest，保持可回滚旧 artifact。

## 停用与回滚

逐命令停止传 `--profile NAME` 即停止手动 opt-in。使用
`loopx extension disable loopx-codex-provider-routing --execute --format json`
停用 managed extension，并用 `loopx extension list --format json` 读回。需要卸载包时，在
专用 Python 环境执行 `python3 -m pip uninstall loopx-codex-provider-routing`；这些命令不会
停止独立 CPA service。operator 只恢复 checked snapshot 的白名单 artifact 与路由 metadata，
保留最新 OAuth refresh token；新加入 credential 保留但按旧快照恢复停用状态。
安装 profile 的回滚只碰那个独立文件，不碰 `config.toml`、`auth.json`、rollouts 或数据库。
不得为刷新目录删除 session、复制数据库、替换真实 CODEX_HOME 或回滚旧 token。
恢复凭据路由字段前，必须先停止自己拥有的 CPA writer；运行中或无法确认 PID 的 writer 会在任何写入前阻断回滚。纯 profile/catalog 快照排除凭据与 slot 状态，无须停止 CPA。
独立 CPA 重启/停止只影响 owner 授权的进程，不改变其他客户端部署。

## 可观测性与公开边界

本阶段用 symbolic receipt、配置检查和 qualification checks 定位问题，无额外后台轮询。
后续在线验收应记录 route/revision、CLI/CPA pin、applied/observed 区分、typed failure、bounded
attempt 和 TTFT/总耗时；无流量不等于健康。未量测时不声明 P99/QPS 或失败率改善。

公开输入输出不包含 token/email/auth 文件、原 prompt/history/raw SSE、私有 endpoint、真实
HOME/CODEX_HOME、session id 或原始 management 响应。私有部署证据留在 operator-owned
目录，只把符号结论和已验证能力投影到公共契约。参考来源见 [REFERENCES.md](REFERENCES.md)。
