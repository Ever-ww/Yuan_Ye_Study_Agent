import { expect, test, type Page, type Route } from "@playwright/test";

test.skip(!process.env.YY_E2E_URL, "设置 YY_E2E_URL（包含一次性 bootstrap 参数）后运行浏览器 E2E");

const project = {
  project_id: "e2e-project",
  workspace_id: "e2e-workspace",
  name: "E2E Research",
  path: "YYWorkspace:\\e2e",
  version: 1,
  migration_version: 1,
  created_at: "2026-01-01T00:00:00Z",
  last_opened_at: "2026-01-01T00:00:00Z",
};
const model = {
  profile_id: "default", provider: "mock", model: "mock-model", selected: true,
  reasoning_effort: "low", supported_reasoning_efforts: ["none", "low", "medium", "high"],
  default_reasoning_effort: "low", effective_reasoning_effort: "low",
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function mockProject(page: Page) {
  await page.route("**/api/v1/projects", (route) => json(route, [project]));
  await page.route(/\/api\/v1\/projects\/e2e-project\/models(?:[/?]|$)/, (route) => json(route, [model]));
}

function event(type: string, sequence: number, payload: Record<string, unknown> = {}) {
  return {
    version: 2, event_id: `e2e-${sequence}`, sequence, timestamp: "2026-01-01T00:00:00Z",
    project_id: project.project_id, session_id: "session-e2e", run_id: "run-e2e", type, payload,
  };
}

test.describe("P0 Agent browser workflow", () => {
  test("streams tool process, handles approval, and restores the collapsed process after refresh", async ({ page }) => {
    await mockProject(page);
    await page.route("**/api/v1/status", (route) => json(route, { ready: true, bash_available: true, sandbox_mode: "os" }));
    await page.route(/\/api\/v1\/projects\/e2e-project\/sessions(?:[/?]|$)/, (route) => json(route, route.request().url().includes("session-e2e") ? [
      { role: "user", content: "run a tool", run_id: "run-e2e", record_id: "r1", timestamp: "2026-01-01T00:00:00Z" },
      { role: "tool", name: "current_time", status: "completed", content: "12:00", run_id: "run-e2e", record_id: "r2", timestamp: "2026-01-01T00:00:01Z" },
      { role: "assistant", content: "The tool completed.", run_id: "run-e2e", record_id: "r3", timestamp: "2026-01-01T00:00:02Z" },
    ] : []));
    await page.route("**/api/v1/approvals*", (route) => json(route, []));
    await page.route("**/api/v1/inbox*", (route) => json(route, []));
    await page.route("**/api/v1/runs", (route) => json(route, { run_id: "run-e2e", session_id: "session-e2e" }));
    await page.route("**/api/v1/observer/runs/run-e2e", (route) => json(route, { run_id: "run-e2e", progress_markdown: "Observer 已完成", state: { user_problem: "run a tool", completed_tasks: ["tool"], in_progress_task: "", current_agent_action: "done", intent_alignment: { status: "aligned", reason: "ok" } } }));
    await page.route("**/api/v1/approvals/approval-e2e", (route) => json(route, { approved: true }));

    await page.addInitScript(() => {
      const sockets: Array<{ onopen?: () => void; onmessage?: (event: MessageEvent) => void; onclose?: () => void }> = [];
      class MockSocket {
        onopen?: () => void; onmessage?: (event: MessageEvent) => void; onclose?: () => void;
        constructor(public url: string) { sockets.push(this); setTimeout(() => this.onopen?.(), 0); }
        close() { this.onclose?.(); }
        send() {}
      }
      (window as unknown as { __yyEmit: (value: unknown) => void }).__yyEmit = (value) => {
        sockets.forEach((socket) => socket.onmessage?.({ data: JSON.stringify(value) } as MessageEvent));
      };
      (window as unknown as { WebSocket: typeof WebSocket }).WebSocket = MockSocket as unknown as typeof WebSocket;
    });

    await page.goto("/agent?project=e2e-project");
    await page.getByLabel("发送给 YYAgent").fill("run a tool");
    await page.getByRole("button", { name: "发送" }).click();
    await page.evaluate((value) => (window as unknown as { __yyEmit: (value: unknown) => void }).__yyEmit(value), event("run_started", 1));
    await page.evaluate((value) => (window as unknown as { __yyEmit: (value: unknown) => void }).__yyEmit(value), event("text", 2, { text: "正在准备工具" }));
    await expect(page.getByRole("button", { name: /查看过程/ })).toBeVisible();
    await page.evaluate((value) => (window as unknown as { __yyEmit: (value: unknown) => void }).__yyEmit(value), event("approval_requested", 3, { approval_id: "approval-e2e", tool_name: "current_time", arguments: {} }));
    await expect(page.getByRole("button", { name: "允许" })).toBeVisible();
    await page.getByRole("button", { name: "允许" }).click();
    await page.evaluate((value) => (window as unknown as { __yyEmit: (value: unknown) => void }).__yyEmit(value), event("tool_completed", 4, { name: "current_time", result: "12:00" }));
    await page.evaluate((value) => (window as unknown as { __yyEmit: (value: unknown) => void }).__yyEmit(value), event("run_completed", 5, { answer: "现在是 12:00。" }));
    await expect(page.getByText("现在是 12:00。", { exact: true })).toBeVisible();
    await expect(page).toHaveURL(/session=session-e2e/);
    await expect(page.getByRole("button", { name: /查看过程/ })).toBeVisible();
    await page.getByRole("button", { name: /查看过程/ }).click();
    await expect(page.getByText(/current_time/).first()).toBeVisible();

    await page.reload();
    await page.waitForLoadState("networkidle");
    await expect(page.getByRole("button", { name: /查看过程/ })).toBeVisible();
    await page.getByRole("button", { name: /查看过程/ }).click();
    await expect(page.getByText("12:00", { exact: true })).toBeVisible();
    await expect(page.getByText("The tool completed.", { exact: true })).toBeVisible();
  });
});

test.describe("P1 Read and Note browser workflows", () => {
  test("keeps Read selection data while the PDF visual selection is cleared and streams an LLM translation", async ({ page }) => {
    await mockProject(page);
    await page.addInitScript(() => {
      localStorage.setItem("yyagent.reader.translation-engine", "llm");
      localStorage.setItem("yyagent.reader.auto-translate", "true");
    });
    const paper = { paper_id: "paper-e2e", title: "A Test Paper", abstract: "A paper for browser tests", publication_year: 2026, venue: "Test", citation_key: "test", authors: [{ display_name: "YY" }], tags: [], status: "active", has_pdf: true, content_hash: "hash-e2e", summary_status: "completed", updated_at: "2026-01-01T00:00:00Z" };
    let papersRequested = false;
    await page.route(/\/api\/v1\/library\/papers(?:[/?]|$)/, (route) => {
      if (route.request().url().includes("/paper-e2e/")) return route.fallback();
      papersRequested = true;
      return json(route, [paper]);
    });
    await page.route(/\/api\/v1\/library\/papers\/paper-e2e\/summary(?:[/?]|$)/, (route) => json(route, { paper_id: paper.paper_id, status: "completed", content: "| A | B |\n| --- | --- |\n| 1 | 2 |", run_id: null, error: null, updated_at: "2026-01-01T00:00:00Z" }));
    await page.route(/\/api\/v1\/library\/papers\/paper-e2e\/inline-questions(?:[/?]|$)/, (route) => json(route, []));
    await page.route(/\/api\/v1\/library\/papers\/paper-e2e\/pdf(?:[/?]|$)/, (route) => route.fulfill({ contentType: "application/pdf", body: buildPdf("Selectable translation text") }));
    await page.route("**/api/v1/translate/llm", (route) => route.fulfill({ contentType: "text/event-stream", body: 'data: {"type":"text","content":"翻译"}\n\ndata: {"type":"text","content":"结果"}\n\ndata: {"type":"done"}\n\n' }));

    await page.goto("/read?project=e2e-project&paper=paper-e2e");
    await expect.poll(() => papersRequested).toBe(true);
    await expect(page.getByText(/A Test Paper/)).toBeVisible();
    await expect(page.locator(".textLayer span").first()).toBeVisible({ timeout: 15_000 });
    await page.evaluate(() => {
      const span = document.querySelector<HTMLElement>(".textLayer span");
      if (!span) throw new Error("PDF text layer did not render");
      const range = document.createRange(); range.selectNodeContents(span);
      const selection = window.getSelection(); selection?.removeAllRanges(); selection?.addRange(range);
      span.closest<HTMLElement>("[data-pdf-page]")?.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    });
    await expect(page.locator(".selection-popover").getByRole("button", { name: "复制", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "笔记" })).toBeVisible();
    await expect(page.getByRole("button", { name: "询问" })).toBeVisible();
    await page.mouse.click(300, 300);
    await expect(page.getByRole("button", { name: "询问" })).not.toBeVisible();
    await expect(page.getByText("翻译结果", { exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(".translation-source")).toHaveText("Selectable translation text");
  });

  test("edits a Note document and autosaves through the Gateway API", async ({ page }) => {
    await mockProject(page);
    const note = { note_id: "note-e2e", kind: "note", name: "实验记录", parent_id: null, path: "notes/实验记录.md", file_path: "notes/实验记录.md", content: "# 实验记录\n\n初始内容", etag: "etag-1", tags: [], links: [], backlinks: [], created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" };
    let saved = "";
    let detailRequested = false;
    await page.route(/\/api\/v1\/projects\/e2e-project\/notes-trash(?:[/?]|$)/, (route) => json(route, []));
    await page.route(/\/api\/v1\/projects\/e2e-project\/notes\/note-e2e\/revisions(?:[/?]|$)/, (route) => json(route, []));
    await page.route(/\/api\/v1\/projects\/e2e-project\/notes(?:[/?]|$)/, async (route) => {
      const request = route.request();
      if (request.method() === "PATCH") { saved = String((await request.postDataJSON()).content || ""); return json(route, { ...note, content: saved, etag: "etag-2" }); }
      if (request.url().includes("/notes/note-e2e")) { detailRequested = true; return json(route, note); }
      return json(route, [note]);
    });
    await page.goto("/note?project=e2e-project&note=note-e2e");
    await expect.poll(() => detailRequested).toBe(true);
    await expect(page.getByRole("textbox", { name: "笔记标题", exact: true })).toHaveValue("实验记录");
    await page.getByRole("tab", { name: "Markdown" }).click();
    const editor = page.getByRole("textbox", { name: "Markdown 源码" });
    await editor.fill("# 实验记录\n\n已通过 Playwright 保存");
    await expect.poll(() => saved).toContain("已通过 Playwright 保存");
    await expect(page.getByText("已保存").or(page.getByText("正在保存"))).toBeVisible();
  });
});

test.describe("P1 Code browser workflow", () => {
  test("creates and deletes an isolated Code session without exposing host paths", async ({ page }) => {
    await mockProject(page);
    const session = { code_session_id: "code-e2e", project_id: project.project_id, status: "active", branch: "yy-code-e2e", base_commit: "abc123", verified_turns: 0, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", logical_roots: { source: "YYAgentSource:\\", skills: "YYSkills:\\", hooks: "YYHooks:\\" } };
    let created = false; let deleted = false;
    await page.route(/\/api\/v1\/code\/sessions(?:[/?]|$)/, async (route) => {
      if (route.request().method() === "POST") { created = true; return json(route, session); }
      if (route.request().method() === "DELETE") { deleted = true; return json(route, { deleted: true }); }
      return json(route, created && !deleted ? [session] : []);
    });
    await page.route(/\/api\/v1\/code\/sessions\/code-e2e\/events(?:[/?]|$)/, (route) => json(route, []));
    await page.goto("/code?project=e2e-project");
    await page.getByRole("button", { name: "创建 Coding Session" }).click();
    await expect(page.getByRole("heading", { name: "yy-code-e2e" })).toBeVisible();
    await expect(page.getByText(/YYAgentSource:/)).toBeVisible();
    page.on("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: /^删除$/ }).click();
    await page.getByRole("button", { name: "确认" }).click();
    await expect.poll(() => deleted).toBe(true);
    await expect(page.getByText("创建 Coding Session", { exact: true })).toBeVisible();
    await expect(page.locator("body")).not.toContainText("C:\\");
  });

  test("keeps Dream controls separate from the Code session surface", async ({ page }) => {
    await mockProject(page);
    await page.route(/\/api\/v1\/dream\/status(?:[/?]|$)/, (route) => json(route, { status: "idle", last_run: null }));
    await page.route(/\/api\/v1\/harness\/dream\/status(?:[/?]|$)/, (route) => json(route, { status: "ready", frozen: false }));
    await page.route(/\/api\/v1\/inbox(?:[/?]|$)/, (route) => json(route, []));
    await page.goto("/operations?section=dream&project=e2e-project");
    await expect(page.getByRole("heading", { name: "Daily Dream", exact: true }).first()).toBeVisible();
    await expect(page.getByRole("heading", { name: "Harness Dream", exact: true }).first()).toBeVisible();
    await page.goto("/code?project=e2e-project");
    await expect(page.locator(".code-workspace").getByText(/每个 Code、Dream/)).toBeVisible();
    await expect(page.locator(".code-workspace").getByText(/Observer/)).toBeVisible();
  });
});

function buildPdf(text: string): string {
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    `<< /Length ${text.length + 36} >>\nstream\nBT /F1 20 Tf 72 700 Td (${text}) Tj ET\nendstream`,
  ];
  let pdf = "%PDF-1.4\n"; const offsets = [0];
  for (let index = 0; index < objects.length; index += 1) { offsets.push(pdf.length); pdf += `${index + 1} 0 obj\n${objects[index]}\nendobj\n`; }
  const xref = pdf.length; pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.slice(1).map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`).join("")}trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
  return pdf;
}
