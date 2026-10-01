# Codex App 入口退役与迁移

LoopX 已退役 Codex App 的 onboarding、activation 和 automation 接入，包括旧 SSH surface。本页
保留原章节路径，用于把旧书签和站内链接导向当前支持的入口；它不再提供 App 安装、SSH 或
heartbeat 操作说明。

## 普通项目改用 Codex CLI

本地项目请继续阅读[从 Codex CLI 启动](./07-codex-cli.md)。Codex CLI 是当前默认的可见、可中断
入口，并继续使用同一套项目 Goal、Todo、Gate、evidence 和 quota 合同。

已有项目状态不需要迁移或重建。切换前只需确认当前 checkout、registry、`goal_id` 和
`agent_id` 与原任务一致，并检查没有另一个执行者持有同一 Todo 的 claim 或 lease：

```bash
loopx status --goal-id <goal-id>
loopx history --goal-id <goal-id> --limit 10
```

若项目尚未连接，按 CLI 章节运行 guided start；不要从旧文档恢复 App automation、旧 activation
命令或 App 专属 scheduler 配置。

## 旧说明如何映射

| 旧说明 | 当前做法 |
| --- | --- |
| 在本地 App 中接入项目 | 使用 Codex CLI guided start |
| 安装或激活 App automation | 已退役，不要恢复 |
| 从 App scheduler 读取 cadence | 已退役；改用可见的 Codex CLI |
| 在 App 与 CLI 间切换 | 直接改用 Codex CLI，并显式处理 identity 与 lease |
| 通过 App 的旧 SSH 方式工作 | 已退役；改用 Codex CLI |

如果其他页面仍给出本地 App activation、automation 或 SSH 操作步骤，应按文档漂移处理；
请改用当前 Codex CLI 章节和 `--help`。
