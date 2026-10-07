// Public URLs are independent of the local preview's deployment base.
export const siteUrl = "https://loopx-project.github.io/loopx/";
export const pageMetadata = {
  home: {
    path: "",
    en: {
      title: "LoopX — Stateful control plane for long-running AI agents",
      description: "Keep long-running AI agents on track across sessions with durable goals, tasks, human approval gates and recovery. Works with Codex, Claude Code and other agent runtimes.",
    },
    zh: {
      title: "LoopX — 长程 AI Agent 的有状态控制面",
      description: "让 Codex、Claude Code 等 Agent 跨会话持续推进任务，用持久目标、任务看板、人工审批与恢复机制管理长程工作。",
    },
  },
} as const;
export type PublicPage = keyof typeof pageMetadata;
