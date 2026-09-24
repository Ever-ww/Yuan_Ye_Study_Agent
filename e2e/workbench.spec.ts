import { expect, test } from "@playwright/test";

test.skip(!process.env.YY_E2E_URL, "设置 YY_E2E_URL（包含一次性 bootstrap 参数）后运行浏览器 E2E");

test.describe("YYAgent workbench routes", () => {
  test("Agent shell loads and command palette is usable", async ({ page }) => {
    await page.goto("/agent");
    await expect(page.getByRole("navigation", { name: "工作台模式" })).toBeVisible();
    await expect(page.locator("#main-content")).toBeVisible();

    await page.getByRole("button", { name: "搜索与命令" }).click();
    const dialog = page.getByRole("dialog", { name: "搜索与命令" });
    await expect(dialog).toBeVisible();
    await expect(page.getByRole("searchbox", { name: "搜索页面或命令" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
  });

  for (const route of ["/read", "/write", "/note", "/code", "/operations", "/capabilities"]) {
    test(`${route} renders a usable shell`, async ({ page }) => {
      await page.goto(route);
      await expect(page.getByRole("navigation", { name: "工作台模式" })).toBeVisible();
      await expect(page.locator("#main-content")).toBeVisible();
      await expect(page.locator("body")).not.toContainText("工作台无法继续显示");
    });
  }

  test("context sidebar can be collapsed and reopened", async ({ page }) => {
    await page.goto("/agent");
    const collapse = page.getByRole("button", { name: "收起侧边栏" });
    await expect(collapse).toBeVisible();
    await collapse.click();
    await expect(page.getByRole("button", { name: "展开侧边栏" })).toBeVisible();
    await page.getByRole("button", { name: "展开侧边栏" }).click();
    await expect(page.getByRole("button", { name: "收起侧边栏" })).toBeVisible();
  });
});
