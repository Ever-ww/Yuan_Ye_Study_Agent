import { expect, test, type Page, type Route } from "@playwright/test";

test.skip(!process.env.YY_E2E_URL && process.env.YY_E2E_MOCK !== "1", "Set YY_E2E_URL or YY_E2E_MOCK=1 to run browser E2E");

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

const folderNames = [
  "00 Inbox", "10 Daily", "20 Research", "30 Projects", "40 Experiments",
  "50 Literature", "60 Meetings", "70 Resources", "90 Templates", "99 Archive",
];

const folders = folderNames.map((name, index) => ({
  note_id: `folder-${index}`,
  kind: "folder",
  name,
  parent_id: null,
  path: name,
  content: "",
  etag: "",
  file_path: `${name}/`,
  tags: [],
  links: [],
  backlinks: [],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
}));

const note = {
  note_id: "note-e2e",
  kind: "note",
  name: "实验记录",
  parent_id: "folder-4",
  path: "40 Experiments / 实验记录",
  file_path: "notes/40 Experiments/实验记录.md",
  content: "# 实验记录\n\n初始内容\n\n| 指标 | 数值 |\n| --- | --- |\n| AUROC | 0.91 |",
  etag: "etag-1",
  tags: ["research"],
  links: [],
  backlinks: [],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function mockSockets(page: Page) {
  await page.addInitScript(() => {
    class MockSocket {
      onopen?: () => void;
      onmessage?: (event: MessageEvent) => void;
      onclose?: () => void;
      constructor() { setTimeout(() => this.onopen?.(), 0); }
      close() { this.onclose?.(); }
      send() {}
    }
    (window as unknown as { WebSocket: typeof WebSocket }).WebSocket = MockSocket as unknown as typeof WebSocket;
  });
}

async function mockNoteApi(page: Page) {
  let current = { ...note };
  let extraFolder: typeof folders[number] | null = null;
  let trashed = false;

  await page.route("**/api/v1/bootstrap", (route) => json(route, { csrf: "e2e-csrf" }));
  await page.route("**/api/v1/projects", (route) => json(route, [project]));
  await page.route("**/api/v1/status", (route) => json(route, { ready: true, sandbox_mode: "os" }));
  await page.route(/\/api\/v1\/projects\/e2e-project\/models(?:[/?]|$)/, (route) => json(route, [{
    profile_id: "default", provider: "mock", model: "mock-model", selected: true,
    reasoning_effort: "low", supported_reasoning_efforts: ["none", "low", "medium", "high"],
    default_reasoning_effort: "low", effective_reasoning_effort: "low",
  }]));
  await page.route(/\/api\/v1\/projects\/e2e-project\/notes-trash(?:[/?]|$)/, (route) => json(route, trashed ? [{
    trash_id: "trash-e2e", original_note_id: note.note_id, name: note.name, kind: "note",
    original_file_path: "40 Experiments/实验记录.md", deleted_at: "2026-01-01T00:00:00Z",
  }] : []));
  await page.route(/\/api\/v1\/projects\/e2e-project\/notes\/note-e2e\/revisions(?:[/?]|$)/, (route) => json(route, [{
    revision_id: "20260101T000000.000000Z-deadbeefdead", created_at: "2026-01-01T00:00:00Z",
  }]));
  await page.route(/\/api\/v1\/projects\/e2e-project\/notes(?:[/?]|$)/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "POST" && url.pathname.endsWith("/initialize-structure")) {
      return json(route, { created: [], folders: [...folders, ...(extraFolder ? [extraFolder] : []), current] });
    }
    if (request.method() === "POST" && url.pathname.endsWith("/move")) {
      const payload = await request.postDataJSON() as { parent_id?: string | null };
      current = { ...current, parent_id: payload.parent_id ?? null, path: payload.parent_id === "folder-2" ? "20 Research / 实验记录" : current.path };
      return json(route, current);
    }
    if (request.method() === "POST" && url.pathname.endsWith("/folders")) {
      extraFolder = { ...folders[0], note_id: "folder-extra", name: "80 Review", path: "80 Review", file_path: "80 Review/" };
      return json(route, extraFolder);
    }
    if (request.method() === "PATCH") {
      const payload = await request.postDataJSON() as { content?: string; name?: string };
      current = { ...current, ...payload, etag: "etag-2" };
      return json(route, current);
    }
    if (request.method() === "DELETE") {
      trashed = true;
      return json(route, { deleted: true, trash_id: "trash-e2e" });
    }
    if (request.method() === "GET" && url.pathname.endsWith("/notes/note-e2e")) return json(route, current);
    return json(route, [...folders, ...(extraFolder ? [extraFolder] : []), ...(trashed ? [] : [current])]);
  });
  await page.route(/\/api\/v1\/projects\/e2e-project\/notes-trash\/trash-e2e\/restore/, (route) => {
    trashed = false;
    return json(route, current);
  });
  await mockSockets(page);
}

test.describe("Note Workspace folder structure", () => {
  test("initializes the research vault folders and keeps Note data in Note mode", async ({ page }) => {
    await mockNoteApi(page);
    await page.goto("/note?project=e2e-project&note=note-e2e");
    await expect(page.locator(".note-sidebar")).toBeVisible();
    await expect(page.locator(".note-tree").getByText("00 Inbox", { exact: true })).toBeVisible();
    await expect(page.locator(".note-tree").getByText("20 Research", { exact: true })).toBeVisible();
    await expect(page.locator(".note-tree").getByText("40 Experiments", { exact: true })).toBeVisible();
    await expect(page.locator(".note-document-title")).toHaveValue("实验记录");
    await page.locator(".note-tree").getByText("40 Experiments", { exact: true }).click();
    await expect(page.locator(".note-tree-item.note")).toContainText("实验记录");
  });

  test("creates a folder, edits Markdown, and autosaves through the Gateway", async ({ page }) => {
    await mockNoteApi(page);
    let patchSeen = false;
    page.on("request", (request) => { if (request.method() === "PATCH" && request.url().includes("/notes/note-e2e")) patchSeen = true; });
    await page.goto("/note?project=e2e-project&note=note-e2e");
    await page.once("dialog", (dialog) => void dialog.accept("80 Review"));
    await page.locator(".sidebar-heading-actions button").nth(1).click();
    await expect(page.locator(".note-tree").getByText("80 Review", { exact: true })).toBeVisible();
    await page.locator(".note-mode-switch button").nth(1).click();
    const editor = page.getByRole("textbox", { name: "Markdown 源码" });
    await editor.fill("# 实验记录\n\n已通过 Playwright 保存");
    await expect.poll(() => patchSeen).toBe(true);
  });

  test("renders Markdown tables in document mode and exposes the source mode", async ({ page }) => {
    await mockNoteApi(page);
    await page.goto("/note?project=e2e-project&note=note-e2e");
    await page.locator(".note-mode-switch button").nth(1).click();
    await expect(page.locator(".note-source-editor")).toContainText("| 指标 | 数值 |");
    await page.locator(".note-mode-switch button").nth(0).click();
    await expect(page.locator(".tiptap-note-content table")).toBeVisible();
    await expect(page.locator(".tiptap-note-content")).toContainText("AUROC");
  });

  test("moves a note into a folder and supports trash restore", async ({ page }) => {
    await mockNoteApi(page);
    await page.goto("/note?project=e2e-project&note=note-e2e");
    await page.locator(".note-move select").selectOption("folder-2");
    await page.locator(".note-move button").click();
    await page.once("dialog", (dialog) => void dialog.accept());
    await page.locator(".inspector-content .danger-button").click();
    await expect(page.getByText("回收站", { exact: true })).toBeVisible();
    await page.locator('.note-revisions button[title]').click();
    await expect(page.locator(".note-document-title")).toHaveValue("实验记录");
  });
});

test.describe("Write and Note boundary", () => {
  test("does not expose Note files in the LaTeX Write tree", async ({ page }) => {
    await mockNoteApi(page);
    await page.route(/\/api\/v1\/projects\/e2e-project\/workspace\/tree(?:[/?]|$)/, (route) => json(route, {
      path: "YYWorkspace:\\",
      entries: [
        { name: "notes", path: "YYWorkspace:\\notes", kind: "directory", blocked: false, size: 0, modified_at: 0 },
        { name: "paper", path: "YYWorkspace:\\paper", kind: "directory", blocked: false, size: 0, modified_at: 0 },
        { name: "main.tex", path: "YYWorkspace:\\main.tex", kind: "file", blocked: false, size: 20, modified_at: 0 },
      ],
      next_cursor: null,
    }));
    await page.route(/\/api\/v1\/projects\/e2e-project\/workspace\/changes/, (route) => json(route, {
      available: true,
      changes: [{ status: "M", path: "YYWorkspace:\\notes\\实验记录.md" }, { status: "M", path: "YYWorkspace:\\main.tex" }],
    }));
    await page.route(/\/api\/v1\/projects\/e2e-project\/workspace\/files(?:[/?]|$)/, (route) => json(route, {
      path: "YYWorkspace:\\main.tex", content: "\\documentclass{article}\n\\begin{document}\nE2E\n\\end{document}",
      etag: "etag-main", workspace_revision: 1,
    }));
    await page.goto("/write?project=e2e-project");
    await expect(page.locator(".workspace-tree")).toBeVisible();
    await expect(page.locator(".workspace-tree")).not.toContainText("notes");
    await page.getByRole("treeitem").filter({ hasText: "main.tex" }).getByRole("button").click();
    await expect(page.locator(".workspace-changes")).toContainText("main.tex");
    await expect(page.locator(".workspace-changes")).not.toContainText("实验记录");
  });
});
