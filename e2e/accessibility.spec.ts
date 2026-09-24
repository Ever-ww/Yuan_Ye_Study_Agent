import { expect, test } from "@playwright/test";

test.skip(!process.env.YY_E2E_URL, "设置 YY_E2E_URL（包含一次性 bootstrap 参数）后运行浏览器 E2E");

test.describe("YYAgent accessibility and responsive behavior", () => {
  test("keyboard focus reaches command search and Escape closes it", async ({ page }) => {
    await page.goto("/agent");
    await page.keyboard.press("Control+K");
    const input = page.getByRole("searchbox", { name: "搜索页面或命令" });
    await expect(input).toBeFocused();
    await page.keyboard.type("Read");
    await expect(page.getByRole("option", { name: /Read/ })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(input).not.toBeVisible();
  });

  test("icon controls expose tooltip text", async ({ page }) => {
    await page.goto("/agent");
    const collapse = page.getByRole("button", { name: "收起侧边栏" });
    await expect(collapse).toHaveAttribute("title", "收起侧边栏");
    await expect(collapse).toHaveAttribute("aria-expanded", "true");
  });

  test("reduced motion remains usable", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/agent");
    await page.waitForLoadState("networkidle");
    await expect(page.locator("#main-content")).toBeVisible();
    const motion = await page.evaluate(() => {
      const style = getComputedStyle(document.documentElement);
      const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
      return { reduced, reducedDuration: style.getPropertyValue("--motion-reduced-duration").trim() };
    });
    expect(motion.reduced).toBe(true);
    expect(motion.reducedDuration).toMatch(/^0?\.01ms$/);
  });

  for (const width of [320, 768, 1024, 1440]) {
    test(`${width}px viewport does not overflow horizontally`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/agent");
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow).toBeLessThanOrEqual(1);
    });
  }
});
