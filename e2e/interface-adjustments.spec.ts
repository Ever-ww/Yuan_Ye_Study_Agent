import { expect, test, type Page, type Route } from "@playwright/test";

test.skip(!process.env.YY_E2E_URL, "设置 YY_E2E_URL 后运行浏览器 E2E");

async function mockWorkbench(page: Page) {
  await page.route("**/api/v1/**", (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = [];
    if (path === "/api/v1/bootstrap") body = { csrf: "e2e-csrf" };
    else if (path === "/api/v1/status") body = { ready: true, bash_available: true, sandbox_mode: "os" };
    else if (path === "/api/v1/projects") body = [{ project_id: "e2e-project", name: "Research" }];
    else if (path.endsWith("/models")) body = [{ profile_id: "default", provider: "mock", model: "mock", selected: true, reasoning_effort: "low", supported_reasoning_efforts: ["none", "low"] }];
    else if (path === "/api/v1/runtime/plugins/status") body = { revision: 0, plugins: [
      { plugin_id: "sample.ready", display_name: "可管理示例", description: "已提供资源", configurable: true, toggleable: true, available: true, enabled: true, desired_enabled: true },
      { plugin_id: "sample.core", display_name: "基础示例", description: "运行时组件", configurable: false, toggleable: false, available: true, enabled: true, desired_enabled: true },
      { plugin_id: "sample.missing", display_name: "待安装示例", description: "尚缺资源目录", configurable: true, toggleable: false, available: false, enabled: false, desired_enabled: false },
    ] };
    else if (path.endsWith("/workspace/tree")) body = { path: "YYWorkspace:\\", entries: [], next_cursor: null };
    else if (path.endsWith("/workspace/changes")) body = { changes: [], revision: 0 };
    else if (path.endsWith("/notes/initialize-structure")) body = { created: [], folders: [] };
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
}

test("Agent 只在点击新建后显示空会话，删除和刷新均不会恢复", async ({ page }) => {
  await mockWorkbench(page);
  await page.goto("/agent?project=e2e-project");
  const sessions = page.getByRole("navigation", { name: "最近会话" });
  await expect(sessions.getByText("新会话", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "开始新会话" }).click();
  await expect(sessions.getByText("新会话", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "删除空白会话" }).click();
  await expect(sessions.getByText("新会话", { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(sessions.getByText("新会话", { exact: true })).toHaveCount(0);
});

test("Read 只保留侧栏内收起按钮，导入为带提示的图标", async ({ page }) => {
  await mockWorkbench(page);
  await page.goto("/read?project=e2e-project");
  await expect(page.getByRole("button", { name: "收起侧边栏" })).toHaveCount(1);
  await expect(page.locator(".reader-toolbar").getByRole("button", { name: /论文栏/ })).toHaveCount(0);
  const importButton = page.getByRole("button", { name: "导入论文" });
  await expect(importButton).toHaveText("");
  await expect(importButton).toHaveAttribute("title", "导入论文");
  const search = page.getByRole("button", { name: "全文搜索" });
  await search.hover();
  await expect(search).toHaveAttribute("title", "全文搜索");
});

test("插件三个分组同级展示，普通按钮也获得悬停说明", async ({ page }) => {
  await mockWorkbench(page);
  await page.goto("/capabilities?view=plugins");
  await expect(page.getByRole("heading", { name: "可管理插件" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "运行基础组件" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "未安装", exact: true })).toBeVisible();
  const missing = page.getByRole("button", { name: /待安装示例/ });
  await missing.hover();
  await expect(missing).toHaveAttribute("title", "待安装示例");
  await expect(page.getByText("尚未提供代码或资源")).toBeVisible();
});

test("各模式可见按钮均有悬停文字", async ({ page }) => {
  await mockWorkbench(page);
  for (const path of ["/agent", "/read", "/write", "/note", "/code", "/operations", "/capabilities?view=plugins"]) {
    await page.goto(`${path}${path.includes("?") ? "&" : "?"}project=e2e-project`);
    await expect(page.locator("#main-content"), path).toBeVisible();
    await page.waitForLoadState("networkidle");
    const unlabeled = await page.evaluate(() => Array.from(document.querySelectorAll<HTMLElement>("button, [role='button']"))
      .filter((button) => button.getClientRects().length > 0)
      .flatMap((button) => {
        button.dispatchEvent(new PointerEvent("pointerover", { bubbles: true }));
        return button.title ? [] : [button.outerHTML.slice(0, 180)];
      }));
    expect(unlabeled, `${path} 包含无悬停文字的按钮`).toEqual([]);
  }
});
