// Role board browser smoke (fork slice S8): renders the Goal "Role board" tab
// from a status fixture carrying a role_v1 `role_board`, and checks stage
// columns, role swimlanes, agent activity chips, the in_review and rejected
// cards, the gates waiting on the user, and both locales. The gate drawer shows
// the plan card's todos, a budget gate's spend and the option its approve
// selects, and polls the open thread so a reply made elsewhere appears without
// reopening it (and refreshes the board row); polling stops when hidden or closed.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const dashboardDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(dashboardDir, "../../..");
const port = Number(process.env.LOOPX_ROLE_BOARD_PORT ?? "5213");
const GOAL_TITLE = "Pilot todo app";

function loadPlaywright() {
  try {
    return require("playwright");
  } catch {
    return require(resolve(homedir(), ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"));
  }
}

async function waitForHttp(url) {
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      // Vite has not started accepting requests yet.
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 200));
  }
  throw new Error(`Timed out waiting for ${url}`);
}

function statusFixture() {
  const fixture = structuredClone(require(resolve(repoRoot, "examples/status.example.json")));
  const goal = fixture.run_history.goals.find((item) => item.id === "loopx-meta");
  assert.ok(goal, "Expected the public loopx-meta run-history fixture");
  goal.display_name = GOAL_TITLE;
  goal.coordination = {
    agent_model: "role_v1",
    registered_agents: ["fable-orch", "opus-dev", "opus-dev-2", "codex-acc"],
    agent_roles: { "fable-orch": "orchestrator", "opus-dev": "developer", "opus-dev-2": "developer", "codex-acc": "acceptor" },
  };
  goal.role_board = structuredClone(require("./role-board-fixture.json"));
  // Fork G9: the goal usage strip in the board header.
  goal.turn_usage_summary = {
    schema_version: "loopx_turn_usage_summary_v0", turns: 42, failed_turns: 3, agent_hours: 6.5,
    tokens_total: 1200000, cost_usd: 18.4, cost_estimated_usd: 4.1, unpriced_turns: 0, accepted_todos: 4,
    cost_per_accepted_todo_usd: 4.6, cost_per_accepted_todo_excl_orchestrator_usd: 3.05, orchestrator_cost_usd: 6.2,
    orchestrator_turns: 5, turns_per_accepted_todo: 10.5, last_turn_at: "2026-09-26T10:00:00+00:00",
    by_role: [{ role: "developer", turns: 30, agent_hours: 5, cost_usd: 12.3, cost_estimated_usd: 0 },
      { role: "acceptor", turns: 12, agent_hours: 1.5, cost_usd: 6.1, cost_estimated_usd: 4.1 }],
    budget: { budget_usd: 20, spent_ratio: 0.92 },
  };
  return fixture;
}

// The gate threads the drawer reads (`GET /api/chat/gate-thread`), per gate.
function gateThread(todoId, server) {
  const base = { ok: true, goal_id: "loopx-meta", todo_id: todoId, status: "open", awaiting: "awaiting_user", messages: [] };
  if (todoId === "todo_rb_plan_gate") {
    // Decisions 12 and 40: the plan card's todos and its acceptance-criteria changes.
    return { ...base, kind: "plan_approval", plan_id: "plan_fedcba987654",
      plan: { plan_id: "plan_fedcba987654", status: "pending", revision: 2, title: "Todo app v2", summary: "Contract first, then the API and the web UI.",
        todos: [
          { key: "contract", text: "Write the API contract", required_role: "developer", depends_on: [], task_repositories: ["api"],
            acceptance: "openapi covers CRUD", validation_command: "test -f openapi.yaml" },
          { key: "web", text: "Implement the web UI", required_role: "developer", depends_on: ["contract"], task_repositories: ["web"],
            acceptance: null, validation_command: null },
        ] },
      criteria_changes: [{ todo_id: "todo_rb_review", old: "GET /todos returns 200; tests pass",
        new: "GET /todos returns 200 paged by 50", reason: "the user asked for paging" }] };
  }
  if (todoId === "todo_rb_budget") {
    // Decision 41: a budget_exhausted gate; `server.budgetMessages` grows when "the CLI" replies.
    return { ...base, kind: "budget_exhausted", options: ["raise_budget", "continue_without_limit", "stop_goal"],
      budget_usd: 0.6, spent_usd: 1, spent_ratio: 1.6667, estimated_usd: 0.25, default_raise_usd: 0.9,
      by_role: [{ role: "orchestrator", cost_usd: 0.75, turns: 3 }, { role: "developer", cost_usd: 0.25, turns: 1 }],
      messages: server.budgetMessages };
  }
  return { ...base, kind: "decision" };
}

async function openBoard(browser, url, locale, { openGates = false } = {}) {
  const page = await browser.newPage({ locale, viewport: { width: 1512, height: 900 } });
  const fixture = statusFixture();
  if (openGates) {
    // The gates are open user todos of the goal, so the drawer offers its decisions.
    const queueItem = fixture.attention_queue.items.find((item) => item.goal_id === "loopx-meta");
    const items = fixture.run_history.goals.find((item) => item.id === "loopx-meta").role_board.gates
      .filter((gate) => gate.awaiting === "awaiting_user")
      .map((gate) => ({ goal_id: "loopx-meta", done: false, text: gate.text, todo_id: gate.todo_id, role: "user", status: "open", task_class: "user_gate" }));
    queueItem.user_todos = { source_section: "User Todo", total_count: items.length, open_count: items.length, done_count: 0, items };
  }
  const server = { budgetMessages: [], fixture, gateThreadReads: 0, statusReads: 0 };
  await page.route(`http://127.0.0.1:${port}/status.json**`, (route) => {
    server.statusReads += 1;
    return route.fulfill({ contentType: "application/json", json: server.fixture, status: 200 });
  });
  await page.route("**/api/**", (route) => route.fulfill({ contentType: "application/json", json: { ok: true }, status: 200 }));
  await page.route("**/api/chat/gate-thread?**", (route) => {
    server.gateThreadReads += 1;
    const todoId = new URL(route.request().url()).searchParams.get("todo_id");
    return route.fulfill({ contentType: "application/json", status: 200, json: gateThread(todoId, server) });
  });
  await page.goto(url, { waitUntil: "networkidle" });
  await page.locator(".personal-goal-link").filter({ hasText: GOAL_TITLE }).click();
  page.loopxServer = server;
  return page;
}

async function setPageHidden(page, hidden) {
  await page.evaluate((value) => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => value });
    document.dispatchEvent(new Event("visibilitychange"));
  }, hidden);
}

/**
 * On a writable (local) source: a typed gate's approve names its option, the open
 * thread polls so a reply made elsewhere appears without reopening, a changed
 * thread refreshes the board row, and polling stops while hidden or once closed.
 */
async function checkGateDecisions(browser, locale) {
  const zh = locale === "zh-CN";
  const page = await openBoard(browser, `http://127.0.0.1:${port}/`, locale, { openGates: true });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const api = page.loopxServer;
  await page.getByRole("button", { name: zh ? "角色看板" : "Role board", exact: true }).click();
  const gates = page.getByRole("region", { name: zh ? "等你处理" : "Waiting on you" }).locator(".personal-role-gate");
  const drawer = page.locator("[data-context-drawer]");
  const approve = drawer.locator(".personal-primary-action").first();
  const settled = () => page.waitForFunction(() => {
    const button = document.querySelector("[data-context-drawer] .personal-primary-action");
    return button && !button.disabled;
  });

  await gates.first().click();
  await drawer.locator('[data-testid="gate-plan-card"]').waitFor({ state: "visible" });
  await settled();
  assert.equal(await approve.innerText(), zh ? "查看影响并决定" : "Review impact and decide", "A plan approval keeps the plain review action");
  assert.equal(await approve.getAttribute("data-gate-option"), null, "A plan approval sends no option");

  await gates.nth(1).click();
  await drawer.locator('[data-testid="gate-budget"]').waitFor({ state: "visible" });
  await settled();
  assert.equal(await approve.innerText(), zh ? "批准（raise_budget）" : "Approve (raise_budget)", "Typed approve names its option");
  assert.equal(await approve.getAttribute("data-gate-option"), "raise_budget", "Typed approve previews that option explicitly");
  if (!zh) {
    assert.deepEqual(errors, [], "No page errors");
    await page.close();
    return;
  }

  // A reply lands through the CLI while the drawer stays open: the thread polls, the board row follows.
  const statusReadsBefore = api.statusReads;
  api.budgetMessages = [{ seq: 1, message_id: "m1", author: "orchestrator", agent_id: "fable-orch",
    text: "CLI-REPLY: the orchestrator used $0.75", at: "2026-09-28T10:00:00+00:00" }];
  api.fixture.run_history.goals.find((item) => item.id === "loopx-meta").role_board.gates
    .find((gate) => gate.todo_id === "todo_rb_budget").message_count = 1;
  await page.waitForFunction(() => document.querySelector('[data-testid="gate-thread"]')?.textContent?.includes("CLI-REPLY"), null, { timeout: 9_000 });
  await page.waitForFunction(() => /1 条消息/.test(document.querySelector('.personal-role-gate[data-todo-id="todo_rb_budget"]')?.textContent ?? ""), null, { timeout: 9_000 });
  assert.ok(api.statusReads > statusReadsBefore, "A changed thread refreshes the status projection");

  // Hidden page: no polling; visible again: an immediate read.
  await setPageHidden(page, true);
  const readsWhileHidden = api.gateThreadReads;
  await page.waitForTimeout(5_600);
  assert.equal(api.gateThreadReads, readsWhileHidden, "A hidden page does not poll the thread");
  await setPageHidden(page, false);
  await page.waitForTimeout(500);
  assert.ok(api.gateThreadReads > readsWhileHidden, "Becoming visible re-reads the thread");

  // Closing the drawer stops polling.
  await drawer.locator(".personal-drawer-close").click();
  await drawer.waitFor({ state: "detached" });
  const readsAfterClose = api.gateThreadReads;
  await page.waitForTimeout(5_600);
  assert.equal(api.gateThreadReads, readsAfterClose, "A closed gate drawer stops polling");
  assert.deepEqual(errors, [], "No page errors");
  await page.close();
}

const cell = (page, role, stage) => page.locator(`[data-role-lane="${role}"] .personal-role-cell[data-stage="${stage}"]`);

async function main() {
  const { chromium } = loadPlaywright();
  const viteBin = resolve(dashboardDir, "node_modules/vite/bin/vite.js");
  const server = spawn(process.execPath, [viteBin, "--host", "127.0.0.1", "--port", String(port), "--strictPort", "--force"], {
    cwd: dashboardDir,
    env: { ...process.env },
    stdio: "ignore",
  });
  let browser;
  try {
    const url = `http://127.0.0.1:${port}/?statusUrl=/status.json`;
    await waitForHttp(url);
    browser = await chromium.launch({ headless: true });

    const page = await openBoard(browser, url, "zh-CN");
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.getByRole("button", { name: "角色看板", exact: true }).click();
    const board = page.getByRole("region", { name: "角色看板" });
    await board.waitFor({ state: "visible" });

    const usage = board.locator("[data-role-usage]");
    assert.match(await usage.innerText(), /\$18\.40[\s\S]*含估算 \$4\.10[\s\S]*6\.5[\s\S]*Agent 小时[\s\S]*42[\s\S]*\$4\.60[\s\S]*不含编排 \$3\.05[\s\S]*92%[\s\S]*开发 \$12\.30 · 30/,
      "Usage strip shows cost, estimated part, agent-hours, turns, cost per accepted todo, budget and role split");
    assert.match(await usage.locator('[data-usage-metric="budget"]').getAttribute("class"), /is-near/, "Budget above 80% is flagged");

    // Stage columns, in order, with derived counts.
    const headers = await board.locator(".personal-role-grid-row.is-header [data-stage]").allInnerTexts();
    assert.deepEqual(headers.map((text) => text.replace(/\s+/g, "")),
      ["待分配1", "已分配2", "执行中1", "验收中1", "返工1", "已完成1"], "Stage columns and counts");
    // Swimlanes by role with agent activity chips.
    assert.deepEqual(await board.locator("[data-role-lane]").evaluateAll((rows) => rows.map((row) => row.dataset.roleLane)),
      ["orchestrator", "developer", "acceptor"], "Role swimlanes");
    const chipClass = (agentId) => board.locator(`.personal-role-agent-chip[data-agent-id="${agentId}"]`).getAttribute("class");
    assert.match(await chipClass("opus-dev"), /is-running/, "Running developer chip");
    assert.match(await chipClass("opus-dev-2"), /is-cooldown/, "Cooling-down developer chip");
    assert.match(await chipClass("codex-acc"), /is-unavailable/, "Unavailable acceptor chip");
    assert.match(await chipClass("fable-orch"), /is-idle/, "Idle orchestrator chip");

    // in_review sits with the acceptor; the rejected card is in rework with its badge and repos.
    const review = cell(page, "acceptor", "in_review").locator('[data-todo-id="todo_rb_review"]');
    await review.waitFor({ state: "visible" });
    assert.match(await review.innerText(), /Implement the todo API[\s\S]*opus-dev[\s\S]*api[\s\S]*验收：codex-acc[\s\S]*计划 plan_0123456789ab/, "In-review card");
    const rework = cell(page, "developer", "rework").locator('[data-todo-id="todo_rb_rework"]');
    assert.equal(await rework.locator(".is-rejected").getAttribute("data-reject-count"), "2", "Reject count badge");
    assert.match(await rework.innerText(), /已驳回 ×2[\s\S]*opus-dev-2[\s\S]*api, web/, "Rework card shows badge, agent and repos");
    assert.equal(await cell(page, "developer", "running").locator('[data-todo-id="todo_rb_running"]').count(), 1, "Running card");
    assert.equal(await cell(page, "developer", "planned").locator('[data-todo-id="todo_rb_planned"]').count(), 1, "Planned card");
    assert.match(await cell(page, "developer", "planned").innerText(), /未分配[\s\S]*免验收/, "Planned card is unassigned without acceptance");
    assert.equal(await cell(page, "orchestrator", "assigned").locator('[data-todo-id="todo_rb_orch"]').count(), 1, "Orchestrator card");
    assert.equal(await cell(page, "developer", "done").locator('[data-todo-id="todo_rb_done"]').count(), 1, "Done card");
    assert.match(await board.innerText(), /另有 3 项任务未显示/, "Omitted cards are announced");

    if (process.env.LOOPX_ROLE_BOARD_SCREENSHOT) await page.screenshot({ fullPage: true, path: process.env.LOOPX_ROLE_BOARD_SCREENSHOT });

    // Gates waiting on the user: plan approval first.
    const gates = board.getByRole("region", { name: "等你处理" }).locator(".personal-role-gate");
    assert.deepEqual(await gates.evaluateAll((items) => items.map((item) => [item.dataset.todoId, item.dataset.gateKind])),
      [["todo_rb_plan_gate", "plan_approval"], ["todo_rb_budget", "budget_exhausted"], ["todo_rb_decision", "decision"]],
      "Gate order keeps each gate's real kind");
    assert.match(await gates.first().innerText(), /Todo app v2[\s\S]*计划审批[\s\S]*等你回复[\s\S]*4 项任务 · 第 2 版/, "Plan approval gate");
    assert.match(await gates.nth(1).innerText(), /预算用尽[\s\S]*等你回复/, "Budget gate shows its kind");
    assert.match(await gates.nth(2).innerText(), /决策[\s\S]*编排者回复中[\s\S]*3 条消息/, "Decision gate awaiting the orchestrator");

    // A card's plan link opens the plan's gate in the context drawer.
    await rework.getByRole("button", { name: /计划 plan_fedcba987654/ }).click();
    const drawer = page.locator("[data-context-drawer]");
    await drawer.waitFor({ state: "visible" });
    assert.match(await drawer.innerText(), /Approve plan: Todo app v2/, "Plan link opens the gate");
    // Decision 40: the in-review card flags its pending criteria change; the link opens the
    // plan gate, which shows the old and new criteria side by side.
    const criteriaLink = review.locator('[data-criteria-change-plan="plan_fedcba987654"]');
    assert.match(await criteriaLink.innerText(), /验收标准变更待批准（plan_fedcba987654），暂不验收/, "Criteria change badge");
    await criteriaLink.click();
    const criteriaTable = drawer.locator('[data-testid="gate-criteria-changes"]');
    await criteriaTable.waitFor({ state: "visible" });
    assert.equal(await criteriaTable.locator('[data-side="old"]').innerText(), "GET /todos returns 200; tests pass", "Old criteria");
    assert.equal(await criteriaTable.locator('[data-side="new"]').innerText(), "GET /todos returns 200 paged by 50", "New criteria");
    assert.match(await criteriaTable.innerText(), /当前标准[\s\S]*拟改标准[\s\S]*the user asked for paging/, "Criteria table headers and reason");
    // Decision 12: the plan's todos are reviewable in the drawer, not only its id.
    const planCard = drawer.locator('[data-testid="gate-plan-card"]');
    assert.match(await planCard.innerText(), /Todo app v2[\s\S]*2 项任务 · 第 2 版[\s\S]*Contract first/, "Plan card title, size and summary");
    assert.match(await planCard.locator('[data-plan-key="contract"]').innerText(),
      /contract[\s\S]*Write the API contract[\s\S]*开发[\s\S]*api[\s\S]*验收标准[\s\S]*openapi covers CRUD[\s\S]*验证命令[\s\S]*test -f openapi\.yaml/, "Plan todo contract");
    assert.match(await planCard.locator('[data-plan-key="web"]').innerText(), /Implement the web UI[\s\S]*依赖 contract/, "Plan todo dependency");
    // Decision 41: the budget gate shows its spend (read-only sources too).
    await gates.nth(1).click();
    const budgetFacts = drawer.locator('[data-testid="gate-budget"]');
    await budgetFacts.waitFor({ state: "visible" });
    assert.match(await budgetFacts.innerText(),
      /已花费[\s\S]*\$1\.00 \/ 预算 \$0\.60（167%）[\s\S]*其中估算[\s\S]*\$0\.25[\s\S]*编排 \$0\.75 · 3 个 Turn \/ 开发 \$0\.25 · 1 个 Turn[\s\S]*批准后预算提高到[\s\S]*\$0\.90[\s\S]*不足以覆盖已花费/,
      "Budget spend, estimated part, per-role split, and a default raise flagged as short of the spend");
    // A card opens the Todo drawer.
    await review.getByRole("button", { name: /Implement the todo API/ }).click();
    await page.waitForFunction(() => document.querySelector("[data-context-drawer]")?.textContent?.includes("Implement the todo API"));

    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(200);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 1, `Narrow role board has ${overflow}px page overflow; the grid must scroll inside its region`);
    assert.deepEqual(errors, [], "No page errors");
    await page.close();

    const english = await openBoard(browser, url, "en-US");
    await english.getByRole("button", { name: "Role board", exact: true }).click();
    const englishBoard = english.getByRole("region", { name: "Role board" });
    await englishBoard.waitFor({ state: "visible" });
    assert.match(await englishBoard.innerText(), /Waiting on you[\s\S]*Plan approval[\s\S]*Planned[\s\S]*Assigned[\s\S]*Running[\s\S]*In review[\s\S]*Rework[\s\S]*Done[\s\S]*Rejected ×2/, "English copy");
    assert.match(await englishBoard.locator("[data-role-usage]").innerText(), /incl\. \$4\.10 estimated[\s\S]*agent-hours[\s\S]*per accepted todo \(4\)[\s\S]*\$3\.05 without orchestrator/, "English usage strip");
    const englishGates = englishBoard.getByRole("region", { name: "Waiting on you" }).locator(".personal-role-gate");
    assert.match(await englishGates.nth(1).innerText(), /Budget exhausted/, "English gate kind badge");
    await englishGates.nth(1).click();
    const englishDrawer = english.locator("[data-context-drawer]");
    await englishDrawer.locator('[data-testid="gate-budget"]').waitFor({ state: "visible" });
    assert.match(await englishDrawer.locator('[data-testid="gate-budget"]').innerText(),
      /Spent[\s\S]*\$1\.00 of \$0\.60 \(167%\)[\s\S]*Orchestrator \$0\.75 · 3 Turns[\s\S]*Approve raises it to[\s\S]*\$0\.90[\s\S]*does not cover the spend/, "English budget facts");
    await english.close();

    await checkGateDecisions(browser, "zh-CN");
    await checkGateDecisions(browser, "en-US");
    console.log("role board browser smoke passed");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }
}

await main();
