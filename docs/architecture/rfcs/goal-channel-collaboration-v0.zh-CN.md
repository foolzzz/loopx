# RFC: Goal Channel 协作模型 v0

- 状态：Draft
- 范围：绑定到单个 LoopX goal 的 provider-backed 外部协作通道
- 决策类型：产品架构与分阶段集成契约

## 摘要

本文引入 **Goal Channel** 作为 LoopX 拥有的核心抽象：一个绑定到唯一
goal 的外部协作通道。这个 channel 可以由 Lark/飞书群、Slack channel 或
thread、GitHub issue、Linear thread，或其他 provider surface 承载。
provider 负责消息投递和 UI 原语；LoopX 负责 goal 状态、todos、human
gates、quota、evidence、receipts，以及被接受的状态迁移。

第一阶段 provider 目标是 Lark/飞书：

- 为一个 goal 创建或复用一个群聊；
- 创建或复用一个 Lark Base Kanban 投影；
- 在群里 pin 一条紧凑的控制消息和 Kanban 链接；
- 发送有界 human-gate 通知；
- 将已被 LoopX 接受的状态同步回 Kanban 投影。

channel 不是事实源。它是一个 LoopX goal 的可见协作入口和反馈 surface。

## 问题

LoopX 已经有稳定状态和一些 Lark 专属能力：

- `lark-kanban` 可以把 LoopX todos 和状态投影到 Lark Base；
- Lark 通知代码路径已经在更窄的领域里验证过 send、readback、
  idempotency 和 profile check。

这些能力还没有组合成用户期待的 Claude Tag 类工作流：

1. 在协作 surface 里 mention 一个 bot。
2. 为该目标获取或创建一个隔离的协作通道。
3. 在同一个地方看到进展和 gate。
4. 不离开协作 surface 就能收到 human gate 提问。
5. 仍然由 LoopX 保持权威的 goal、todo、gate 和 evidence 状态。

如果没有一等的 Goal Channel 抽象，Lark 群、Base 看板、消息 thread、
pinned status 和 notification receipt 很容易彼此漂移。

## 目标

- 定义一个 provider-neutral 的 LoopX 概念，表示“这个 goal 的外部协作通道”。
- 让 Lark 群聊、Kanban、pinned message 和 gate notification 都绑定到同一个
  `goal_id`。
- 保持 LoopX 是 canonical goal、todo、gate、evidence 和 quota 状态的唯一写入者。
- 让 provider 写操作显式、可预览、幂等，并经过 readback 验证。
- 允许人在 channel 里看到并回答 gate，但不因此授予 channel 宽泛写权限。
- 后续可以增加 Slack、GitHub 或 Linear 等 provider adapter，而不需要重命名核心概念。

## 非目标

- 替代 `lark-kanban`；Goal Channel 只是组合它。
- 让 Lark、Slack 或任何外部工具成为事实源。
- 在开源 CLI 中内置一个 LoopX 托管的全局 Lark app。
- 要求所有用户或租户共用一个固定 bot 身份。
- 将任意聊天文本直接视为已接受的状态迁移。
- 把原始聊天历史、私有 message id、本地路径、凭据或 raw provider
  payload 复制进公开 packet。
- 在本 RFC 中解决完整远程 runner 编排。

## 命名

核心抽象使用 **Goal Channel**。

不要把 `room` 作为主名称。群聊可以是一个实现细节，但 Goal Channel 可以同时包含
chat、pinned status、Kanban、notification receipts 和 provider-specific metadata。

建议命令面：

```bash
loopx goal-channel setup --provider lark --goal-id <goal-id>
loopx goal-channel target add --name <target> --provider lark ...
loopx goal-channel setup --goal-id <goal-id> --target <target>
loopx goal-channel attach --target <target> --goal-id <goal-a> --goal-id <goal-b>
loopx goal-channel configure --goal-id <goal-id> --auto-notify-human-gates
loopx goal-channel doctor --goal-id <goal-id>
loopx goal-channel sync --goal-id <goal-id>
loopx goal-channel notify-gate --goal-id <goal-id>
```

`goal-channel` 同时作为持久控制面对象和用户可见 CLI。

## 所有权模型

| 能力 | LoopX | Provider channel | Provider adapter |
| --- | --- | --- | --- |
| Goal lifecycle | Owner | Projection | Calls LoopX |
| Todos、claims、gates、quota | Owner | Projection and prompts | Syncs bounded packets |
| Kanban rows | Source data owner | Display owner | Upserts rows |
| Group/chat/thread | References binding | Owner | Creates, updates, reads |
| Pinned status | Builds bounded content | Displays | Sends and pins |
| Human gate question | Owner of question and cooldown | Delivery | Sends and verifies |
| Credentials and profile | Never stores secrets | Provider auth | Uses local-private profile |
| Receipts | Owner of accepted transition receipts | Message ids are private | Records compact send/readback receipt |

provider 可以保存自己的状态。LoopX 只保存运行 channel 所需的最小本地私有绑定。

## Lark Provider 绑定

Lark Goal Channel 绑定是本地私有、项目作用域内的配置：

```json
{
  "schema_version": "loopx_goal_channel_lark_binding_v0",
  "goal_id": "loopx-goal",
  "provider": "lark",
  "enabled": true,
  "channel": {
    "chat_id": "oc_<private-chat-id>",
    "chat_name": "LoopX - loopx-goal",
    "pinned_message_id": "om_<private-message-id>"
  },
  "kanban": {
    "base_token": "<private-base-token>",
    "table_id": "tbl...",
    "view_ids": {
      "Kanban": "vew...",
      "User Gates": "vew..."
    }
  },
  "identity": {
    "mode": "project_bot",
    "sender_profile": "loopx-project-bot",
    "sender_identity": "bot",
    "bot_display_name": "LoopX Bot"
  },
  "receipts": {}
}
```

该文件应位于 `.loopx/` 或其他被忽略的本地私有路径。公开 status packet 不得暴露
chat id、member id、message id、profile name、raw Lark payload、本地文件路径或凭据。
只有当调用方明确选择展示时，公开 packet 才可以展示布尔值、计数、脱敏 provider
label 和 operator-safe URL。

### 共享 provider target

多个 Goal Channel 可以引用同一个具名、本地私有的 provider target。target 持有
可复用的 Lark 群和发送身份；每个 Goal binding 仍独立持有自己的控制消息、Kanban、
receipt 和 cooldown 状态：

```bash
loopx goal-channel target add \
  --name loopx-dev \
  --provider lark \
  --chat-id <private-chat-id> \
  --bot-app-id <private-app-id> \
  --execute

loopx goal-channel attach \
  --target loopx-dev \
  --goal-id goal-a \
  --goal-id goal-b \
  --execute
```

target store 位于解析后的 LoopX runtime root 下，绝不能成为公开或提交进仓库的配置。
引用 target 的 Goal binding 只保存 `target_ref` 和 Goal 本地状态；更新 target 会改变
所有引用 Goal 解析到的群或 sender，但不会合并这些 Goal 的状态。
target 换到另一个群后，应重新执行有界 `attach` 批次，让每个 Goal 在新群中分别建立并
回读自己的控制消息。

不同机器之间不自动同步私有 chat id 或认证 profile。若本机和开发机需要使用同一个群，
应在两台机器上分别配置同名 target。未来接收群回复时，只能接受对具体 gate 消息的回复，
或携带明确 Goal id 的操作；不得把普通群文本推断给多个 Goal 中的某一个。

## BYO Provider Identity

开源 LoopX 应默认使用 **Bring Your Own provider identity**：

- 用户在自己的租户里创建或选择 Lark app / bot；
- 用户通过 `lark-cli` 或未来 provider-specific profile manager 完成认证；
- LoopX 只保存本地 profile 引用和紧凑验证状态；
- LoopX 不把一个固定跨租户 bot 作为隐式依赖。

支持的身份模式：

| 模式 | 适用场景 | 取舍 |
| --- | --- | --- |
| `local_user` | 由用户创建并持有群聊和 Base | 资源归属清晰；消息仍必须由 bot 发送 |
| `project_bot` | 使用项目专属 bot profile 发送 channel 消息 | 需要配置 app/bot，但消息身份稳定 |
| `managed_app` | 未来托管产品 | 体验最好，但需要租户安装、合规和运维 |

第一版实现使用本地 user identity 操作群聊和 Base；Goal Control message、pin
和 gate notification 始终使用已配置的 bot identity，不请求也不依赖
`im:message.send_as_user`。

直接执行 setup 时必须显式传入 `--bot-app-id cli_...`；target 模式则从本地私有
target 取得这项明确选择。LoopX 会验证该 app id
与所选 `lark-cli` profile 一致，再允许加 bot 或发消息。省略该参数只适用于
preview，不代表可以静默选择默认 profile 的 bot。

## 生命周期

### Setup

`goal-channel setup --provider lark --goal-id <goal-id>` 应该：

1. 解析并校验 goal。
2. 加载或创建本地私有 Lark channel 绑定。
3. 验证 `loopx-lark` extension activation 和所需权限。
4. 验证本地 user resource identity 和已配置的 bot sender。
5. 创建或复用一个 Lark 群聊，并验证 bot 已加入群聊。
6. 通过 `lark-kanban` 创建或复用 Lark Kanban Base。
7. 回读并保存 canonical Base URL。
8. 发送一条包含 Kanban 链接的紧凑 Goal Control message。
9. pin 这条已验证的控制消息。
10. 保存本地私有绑定和紧凑 receipt。

默认是 dry-run。外部写操作必须要求 `--execute`。

### Sync

`goal-channel sync` 组合现有投影：

- 用 `lark-kanban sync-loopx-todos` 同步 active user/agent todos 和派生领域 outcome；
- 当 channel 可见摘要发生实质变化时，更新或追加紧凑 status/control message；
- 只有在单独配置后，才启用 periodic report 或 explore projection sink。

sync 命令不得从远端 row 创建新的 canonical todo。

### Human Gate Notification

当 LoopX 已经判定某个 human gate 或 user todo 需要关注时，
`goal-channel notify-gate` 发送有界消息。触发输入来自现有 quota 和
interaction-contract surface：

- `state=operator_gate`；
- `notify_user_on_gate=true`；
- `notify_user_on_open_todo=true`；
- `gate_prompt`；
- `operator_question`；
- `open_todo_notify_reason`；
- `user_todo_summary`；
- `user_gate_notification_cooldown`。

消息包含：

- goal label 和短 objective；
- 具体 gate question；
- 最多三条 user-gate 或 user-action todo；
- 期望回复格式；
- Kanban 链接或 channel control 链接；
- 等待期间的 next safe action（如果存在）。

消息不包含本地路径、raw active state、私有日志、凭据、message id 或 raw provider
payload。

自动投递默认关闭。完成 Goal Channel setup 后，先预览，再显式启用：

```bash
loopx goal-channel configure --goal-id <goal-id> --auto-notify-human-gates
loopx goal-channel configure --goal-id <goal-id> --auto-notify-human-gates --execute
```

启用后，每次成功且非 dry-run 的 `refresh-state` 都会根据 LoopX canonical state
重新计算 quota。只有 quota 选中 human gate 时才发送，并复用 `notify-gate`
已有的 bot 身份校验、语义幂等、冷却、provider idempotency key 和消息回读。

单次 refresh 可使用 `loopx refresh-state ... --suppress-external-sinks`
临时抑制投递，而无需禁用 binding。对带 Turn 绑定的恢复，工具会记住这次暂停；
再次允许外发需使用返回的 `resume_key`，提交 `--resume-external-sinks <resume_key>`。
这只确认恢复当前操作的投递，不授予新权限；无暂停记录的旧操作保留原行为。
详见 [恢复握手](../../state-interaction-model.md)。持久关闭自动投递：

```bash
loopx goal-channel configure --goal-id <goal-id> --no-auto-notify-human-gates --execute
```

该 opt-in 只保存在项目本地私有的 Goal Channel binding 中，不授予仓库或 LoopX
状态迁移权限。群聊回复可以补充 context，但只有经过 LoopX 校验并记录的 decision
才能改变 gate 状态。

自动生命周期投递会先解析已启用且 doctor 验证通过的 Lark extension，再读取私有
binding。启用自动投递时必须使用项目本地 canonical binding 路径；由于
`refresh-state` 没有逐次传入 binding path 的入口，自定义 `--binding-path`
会被拒绝。唯一的恢复例外是显式本地 disable 命令：即使 extension 或 binding
不完整，它也可以清除 opt-in；该路径不会进入 provider 代码，也不会执行外部写。

启用时还会写入一个 owner-only 的本地 marker，其中只包含 enabled boolean。
生命周期在 extension activation 前只允许读取这个 marker，用来区分“从未配置”
和“已配置但 extension 后续不可用”。后者会通过可重试的
`extension_unavailable` postcondition fail closed；marker 不包含 provider id、
凭据、channel metadata 或 raw payload。

## 命令契约

每个 effectful command 返回紧凑 packet：

```json
{
  "schema_version": "loopx_goal_channel_operation_v0",
  "ok": true,
  "goal_id": "loopx-goal",
  "provider": "lark",
  "operation": "notify_gate",
  "execute": true,
  "external_write_performed": true,
  "readback_verified": true,
  "idempotency_key": "sha256:...",
  "receipt_id": "receipt_...",
  "public_summary": "sent one gate notification to the configured Lark channel",
  "private_provider_payload_captured": false
}
```

失败应类型化：

- `extension_unavailable`；
- `provider_identity_unverified`；
- `channel_binding_missing`；
- `channel_membership_unverified`；
- `kanban_binding_missing`；
- `notification_cooldown_active`；
- `readback_mismatch`；
- `state_transition_rejected`；
- `provider_api_failed`。

## 幂等和 Cooldown

provider 写操作使用语义 action 派生 idempotency key，而不是使用本次尝试的时间：

```text
goal_id + provider + operation + todo_id/gate_id + gate_text_hash + channel_id
```

规则：

- 重试同一次发送返回 `already_sent` 或原始 receipt；
- gate 文案变化可以生成新的 notification key；
- cooldown 抑制重复提醒，但不关闭 gate；
- stale provider event 不能覆盖更新的 LoopX revision。

## 安全与隐私

- Channel membership 不是 LoopX 写权限。
- 发送前必须验证 bot membership。
- 记录成功发送 receipt 前必须完成 message readback。
- Raw provider payload 留在本地私有状态。
- 从 shared/global registry 调用时，Goal Channel 状态必须落在所选 goal
  的 canonical `source_registry` 旁边，调用者 CWD 不能作为默认状态根目录。
- 本地私有 JSON 使用同目录、owner-only 的临时文件完成写入，再原子 replace，
  避免中断时暴露或截断旧 binding。
- 本地 checkout 路径、active-state 路径、凭据、chat id、member id、message id
  和 profile name 不进入公开 artifact。
- channel 可以展示 Kanban 链接，但 Kanban 仍然只是投影。
- destructive、credentialed、production、publish、merge 或 external-write gate
  仍然是 LoopX gate，不能被聊天文本绕过。

## 最小可用切片

第一版最小可用实现应包括：

1. 增加 `loopx goal-channel`，包含 `setup`、`configure`、`doctor`、`sync` 和
   `notify-gate`。
2. 只实现 Lark provider。
3. 复用现有 `lark-kanban` setup/sync 和 `loopx-lark` extension activation checks。
4. 为一个已有 goal 创建或复用一个 Lark 群。
5. 发送并 pin 一条紧凑 Goal Control message。
6. 发送带 idempotency 和 readback 的 human-gate notification。
7. 可选地在授权的 `refresh-state` 写回后发送 LoopX 选中的 human gate。
8. 将本地私有绑定、automation opt-in 和 receipt 保存到 `.loopx/`。

这个切片先验证外部协作入口。

## 验证

第一版切片必须证明：

- setup 默认 dry-run，只有带 `--execute` 时才执行外部写；
- 一个 goal 映射到一个本地私有 Lark binding；
- 读取私有配置前会先检查 extension activation；
- Kanban board 可以被复用或创建，然后成功同步；
- Goal Control message 可以发送、pin，并通过 readback 验证；
- human-gate notification 遵守 cooldown 和 idempotency；
- 重试通知不会产生重复可见消息；
- 自动投递默认关闭，并且可按单次 refresh 临时抑制；
- 自动投递读取 canonical quota，非 gate 状态不发送；
- doctor 能用类型化 blocker 报告缺 bot auth、缺 channel、缺 Kanban 或 stale
  extension activation；
- 本地私有 binding 文件保持 ignored 且 untracked；
- 公开 packet 不包含 chat id、member id、message id、profile name、本地路径、
  raw provider payload 或凭据。

## Agent 级对话提案

以下共享会话与入口设计仍为提案。归位到本 RFC 不会启用 adapter、替换已交付的
Goal Channel 行为，也不证明 Web/Lark 收敛。会话身份与模式接入归
[Agent 会话执行模式](agent-session-execution-modes-v0.zh-CN.md)所有。

## Agent 级 Web 与 Lark 收敛

短期协作产品不是一个独立的状态 Bot。它是 Agent 真实工作会话的第二个前端
传输：

```text
LoopX Goal
  -> Agent A
       -> working session A (attached or managed)
            -> Web Chat
            -> Lark Bot connection A
  -> Agent B
       -> working session B (attached or managed)
            -> Web Chat
            -> Lark Bot connection B
```

在 v0 中，每个 Agent 至多有一个活跃的 `lark_bot` 连接。这是一个逻辑上的
Agent 到连接绑定；它不要求每个 Agent 都有唯一的 Lark 应用凭据。如果本地
broker 保留显式的 Agent 与频道路由，一个 Bot 应用可以为多个连接服务。

### 一个有序的工作对话

项目 coordinator 的基线入口就是现有的 **Goal → 对话**。已注册 peer 也可以
承担这份职责；两种选择都不另建 coordinator 对话，也不改变管家的跨 Goal
职责。[Codex 显式续跑](../../reference/goal-chat-continuation.md)复用输入框、
旁的开启/暂停/恢复、流式运行与原本地历史，接入共享委派和独立验收的成员
返回。queue/inbox/steer 保留不同回执；首次配置选择现有执行绑定，不从注册
推导授权。Lark、其他主力驱动等价和无人值守运行仍需分别资格化。

在实时操控和队列会话模式下，Web 与 Lark 消息进入所选 Agent 会话的同一个
串行化入口流。每条消息记录 public-safe 的传输元数据，例如 `origin=web` 或
`origin=lark`，但 origin 不会选择不同的 Agent、对话历史、执行器或 LoopX
状态机。

会话路由器在投递给运行时之前分配顺序。同时到达的 Web 与 Lark 消息可以等待、
通过显式控制动作中断，或按会话策略失败关闭；它们绝不能产生两个并发的 Agent
尝试。响应可以根据连接策略投影到两个 surface，同时保留一个规范序列。

异步 inbox 事件则不同：在被选中的 Agent 排空并解释之前，它仍是 owner-private
的外部输入。只有被接受的、面向 Agent 的消息或由此产生的持久效果才加入
工作会话序列。仅做 provider 采集不会创建对话历史、任务权威、Turn 或 quota
消耗。

### Agent 绑定，而非 Goal 级或运行时专属绑定

Lark 连接绑定到 Goal 内的具体 Agent。如果 Goal 有多个 Agent 而连接没有指明
其中一个，路由失败关闭。Bot 直接与该工作 Agent 对话；它不会先请 manager
Agent 分类或转发消息。绑定不硬编码到 Codex：该 Agent 的执行会话今天可以是
挂接的 Codex App，以后也可以是托管的 Pi/`dsh` 会话。

## Agent 级外部连接器模型

Lark 群入口和 Lark 文档评论是一个 provider-neutral 连接器边界的两个实例。
连接器把外部源绑定到一个已登记的 Agent，并且只宣称它能实际执行的操作：

```text
agent_external_connector_v0 = {
  goal_ref,
  agent_ref,
  provider_kind,
  source_kind,
  source_ref,             // opaque owner-local reference
  capture_policy,
  ingress_policy,
  response_policy,
  cursor_ref,
  lifecycle,
  capabilities[]
}
```

同一个 provider 可以暴露多种 source kind。例如，Lark 群源可以宣称实时投递、
历史追补、thread 回复和 ACK；而文档评论源可以宣称增量列举、锚点与回复链
读回、评论回复和已解决状态观察。缺失的能力保持不可用；LoopX 不会通过抓取
无关 surface 来模拟它们。

Connector capability 还可以暴露类型化的 `permission_requirements`，包含 provider
身份、精确 scope、发布要求，以及绑定所选 App 的官方修复入口。这些事实由
provider 扩展拥有；LoopX 内核只负责渲染类型化指引。实时接收、响应写入和历史
追补是彼此独立的能力，不得压缩成一个笼统的“消息权限”标志。

### 权威材料与协作事件

持久文档与其评论具有不同的权威语义：

- 文档正文被登记为 Goal 权威材料，带有新鲜度、修订、所有者状态和冲突策略；
- 评论是面向 Agent 的 owner-private 外部输入，它本身不是被接受的需求、Todo
  变更或仓库事实；以及
- 纳入一条评论需要一个显式的持久效果，例如 Todo 更新、被接受的设计修订、
  不跟进理由或 owner gate。

读取正文不会推进评论 cursor。列举评论不会让文档变得权威。与已接受状态冲突的
评论被记录为待决决策或证据缺口，而不是静默改变 Goal 事实。

### 捕获、重放与确认

每个事件源连接器拥有稳定的 provider 事件 id、增量 cursor 或等价检查点、
有界的追补策略和幂等键。实时订阅和历史追补进入同一个去重后的 inbox，因此
在挂接前或停机期间创建的事件不会静默丢失。源可以按 mention、作者、文档、
评论状态、锚点或已配置的源范围过滤，而不改变其投递模式。

Agent 按以下顺序处理一个被接受的事件：

```text
capture and deduplicate
  -> mark processing
  -> read fresh Goal and authority state
  -> record durable effect or explicit no-follow-up rationale
  -> send an optional response through a declared Connector capability
  -> verify provider readback
  -> ACK and advance the source cursor
```

任何 ACK 或 cursor 推进都不得先于持久效果和必需的已验证响应。崩溃会幂等地
重放同一事件。私有正文、作者、provider id、源引用和评论文本保留在
owner-local inbox 存储中；状态和 quota 只看到无内容的紧迫性。

### 投递到工作 Agent

连接器捕获与 Agent 投递保持正交。实时群消息可以操控当前工作会话、在其有序
队列中等待，或唤醒异步 Agent inbox。文档评论通常通过 `async_inbox` 进入，
但当显式策略允许时，同一事件也可以提交到已验证的实时会话。在所有情况下，
它都指向已有绑定 Agent，绝不静默启动影子 manager 或全新对话。

### 短期 Goal Channel 桥

现有 Goal Channel 传输可以提供第一条 Lark 投递路径，前提是其 Goal 级连接被
细化为显式目标 Agent，并路由到该 Agent 已有有序会话。这个桥是增量实现路径，
不是保留第二个仅 IM 对话生命周期的许可。

如果该 Agent 级提案被接受，它将细化上文“一个 Goal 对应一个 Lark
绑定”的交互式聊天约束。Goal 级 Kanban、生命周期通知和共享协作工件可以保持
Goal 级；入站工作对话是 Agent 级的。

<a id="agent-scoped-bot-ingress-modes"></a>

## Agent 级 Bot 入口模式

Agent 到 Bot 的连接与 peer 协作需要同样的三种显式入口语义。用户侧简称
**inbox**、**queue**、**steer**，保留下述现有词汇。它们表达同一已绑定 Agent 的
投递意图，不是三个 Agent 或自然语言分类器。本提案将共同策略扩展到 peer 入口，
不因重命名输入就声称新增 API 或改变已有 adapter：

```text
agent_bot_ingress_mode_v0 =
  live_steering
  | session_queue
  | async_inbox
```

三种策略解决不同的可用性条件：

| 模式 | 投递目标 | 可用性模型 | 持久边界 |
|---|---|---|---|
| `live_steering` | 已绑定工作会话中指定的当前执行 | 宿主能在声明的安全点采用输入 | 现有会话/事件存储及消费回执；无第二执行器 |
| `session_queue` | 同一已绑定会话中的后续工作输入 | 当前工作结束或明确交还执行权后再投递 | 按 Agent 与会话键控的 owner-local 持久有序入口队列 |
| `async_inbox` | 显式排空后的下一个合格 LoopX Agent Turn | 无需 Agent 进程保持存活 | 现有 provider 拥有的事件 inbox 加无内容 quota 紧迫性 |

这细化了此前 queue 的“下次接受输入”表述：把 pending 输入合入当前工作的宿主，
并不因此实现拟议 queue 语义。变更必须显式资格化，在 opt-in 实现和兼容测试通过前
保持旧 profile 行为。

沿现有入口身份和接收者范围持久化请求模式、允许的 fallback 和实际投递处置。读回
区分耐久收件、排队派发、宿主消费和 steer 采用；工作采用/验收仍属于 collaboration/work
owner。模型正文或 HTTP 成功不是消费回执；未知能力明确失败。前端、CLI、Lark 在
原工作/对话面展示实际模式、等待原因及结果，不另造一块团队看板。

<a id="delivery-intent-does-not-choose-the-wake-policy"></a>

### 投递意图不决定唤醒策略

Mode 决定输入可以在哪里被消费；binding 已有的续跑 owner 决定是否接纳下一次
执行机会。以下提议矩阵用于资格化 adapter，不增加第四种入口模式：

| 接收方状态 | 要求行为 |
| --- | --- |
| 活跃 turn 或工具未决 | Inbox 等待显式 drain，queue 等待后续 turn；steer 指向精确活跃 generation 和已声明的安全输入边界。收件成功不能证明已提交的模型/工具请求被抢占。 |
| 空闲或 turn 完成 | 保存符合范围的 inbox/queue 输入；只有配置的续跑 owner 在范围/预算检查后才可接纳新 turn，没有该策略就显示待处理。无活跃目标时 steer 不可用。 |
| 正在收尾或已中断 | 保留迟到输入/结果身份，不重新打开收尾 turn；收尾后重查。通知不能撤销显式中断，恢复遵循已有 owner 和暂停策略。 |
| 未加载或断线 | 保存成功不证明 session 在线；仅经资格化的 binding 路径恢复，重验范围与 generation，不支持恢复时保留可行动的待处理/不可用观察。 |

排队不是启动 turn 的承诺，provider trigger 标志也不是 LoopX 准入。多条已收件
消息可以进入同一合格 turn，但独立工作请求仍保留各自身份和返回义务；这些关系
由[交接契约](capable-manager-semantic-handoff-v0.zh-CN.md#团队中的请求身份与结果路由)
拥有，不能把一条传输回执当作 join。

分别投影 ingress 收件回执、实际执行/唤醒观察和工作结果/验收；不能把“消息已存”
显示成“Agent 正在工作”，也不能把唤醒通知显示成“结果已收到”。通知丢失后，保存的
输入/结果仍须可经读回发现；重放或重连须按 ingress/result 身份去重应用，不能启动第二个执行器。在已有
修订输入 fixture 中增加无唤醒策略的空闲输入、收尾竞态、通知合并，以及结果提交到
通知之间重启。这是设计要求，每种宿主仍需独立资格化。

### 捕获、入口与回复正交

provider 选择与 Agent 投递不得复用同一个过载标志。初始 Lark 群形态是：

```text
capture_scope: mentions | configured_chat_all
ingress_mode: live_steering | session_queue | async_inbox
reply_mode: source_thread | topic_reply | configured_mirror
```

`capture_scope` 回答哪些 provider 事件合格。`ingress_mode` 回答一个合格事件
如何到达 Agent。`reply_mode` 回答已验证响应投递到哪里。现有
`incoming_mode=mentions|all` 只表达捕获范围；它不是会话挂接的证据。

持久 Inbox 范围必须与实际 provider 路由范围一致。即使启用了精确源消息回复，
`addressed_only` 流也不得投影成 `thread_complete`。`configured_chat_all` 仍是 owner
的显式选择：它会为领域解释保存已配置会话，但只有 typed question、mention 或
已验证的 Bot reply 才会激活 `reply_due`。

mention 准入同时绑定已验证 provider profile 返回的 App id 与 Bot open id；渲染后
的 display name 只是兼容信号，不能成为唯一身份依据。每个被拒绝的 provider event
都要在 listener health 中保留 `not_addressed`、`topic_mismatch` 等无内容决策原因，
让“已经看到但未持久化”的事件不再隐藏在一个裸 `ignored` 状态后面。

回退是显式的，默认失败关闭。`live_steering` 连接在会话不可用时可以选择加入
`session_queue` 或 `async_inbox`，但不得静默启动另一个运行时，也不得把同一
事件写入多个模式。所选模式、回退决策和去重键产生一个无内容的入口回执。

### 实时操控

`live_steering` 提交到已验证的 Agent 工作会话绑定。它共享 Web 入口串行器、
上游恢复身份、中断策略、工作区、运行时、信任和能力边界。如果该绑定陈旧、
模糊、终态或属于另一个 Agent，投递失败关闭。

操控是传输，不是任务权威。当前会话可以消费只读输入而不领取新工作。实质效果仍然需要与
所采用执行模式相称的最新 LoopX 决策、验证、回写和结算。

Steer 面向当前执行代际及其下一个支持的安全输入点，不等于 interrupt/restart。
外部工具未返回时，宿主可以耐久接收 pending correction，但不能声称已经采用。
无法安全注入时明确报告，仅按请求显式 fallback 处理；绝不伪造工具结果来投递纠正。
失效工具调用需要明确取消处置，其迟到结果对照当前输入版本和执行 fence 对账。
消息本身不取消所有 peer，也不撤销其权限。

### 会话队列

`session_queue` 是已知 Agent 工作会话的 broker 拥有的缓冲。它保留稳定的事件
去重、按会话排序、有界大小、过期、背压、取消和崩溃安全派发。它不是 LoopX
Todo 队列，不得改变 Goal 优先级、认领工作或授予能力。

当前工作结束或明确交还执行权后，broker 通过正常串行化入口提交最旧的合格条目；
仅有 pending-tool idle 观察不能证明这个边界。缺失
或被替换的会话需要显式重新绑定或死信决策；它不会把条目静默路由到全新 Agent
历史。

### 异步 inbox

`async_inbox` 复用现有 Lark 事件 inbox 和 collector，而不是让 Agent 进程保持
存活。collector 写入 owner-private 的有界事件。LoopX 只投影
`operator_inbox_urgency_v0`：pending/question/mention/reply 计数、最旧年龄和
`reply_due`，绝不投影消息正文、发送者、provider id、私有路径或 chat id。

当 `reply_due=true` 时，inbox 通道在下次合格准入时抢占普通推进和 monitor 工作，
不打断当前执行。被选中的 Agent 排空有界内容，对照最新 Goal 状态解释它，先写入
任何持久效果，然后发送至多
一条带 provider readback 的幂等 source-thread 回复，最后才 ACK。仅排空是
只读的；采集或 ACK 永远不是语义权威。

Goal Topic 兼容运行时目前把 provider 采集、Inbox 文件、Goal Chat 回答、回复
和 ACK 内联组合在一起。该路径是有用证据，但当它打开通用 Agent 会话或未能
在绑定 Goal 上登记 inbox 紧迫性时，它不是 Agent 级收敛。实现必须把 provider
采集与入口策略分开、要求已登记的 Agent id，并且要么通过已验证的工作会话
绑定提交，要么把 inbox 指针发布到规范 quota 路径。

用同一修订输入 fixture 验证三模式：未决工具、接收方忙碌/离线、消息过期、满队列、
重复/冲突身份、会话替换、发送方撤权及迟到工具结果。断言实际消费边界和 fallback，
不能只看消息存在。Inbox drain 不证明工作验收；queue 不改当前工作；steer 不能先于
宿主回执声称已采用。这些是拟议验收要求，不是所有宿主已支持三模式的证据。
