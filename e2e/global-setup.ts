import { chromium, type FullConfig } from "@playwright/test";
import { storageState } from "./playwright.config";

export default async function globalSetup(_config: FullConfig): Promise<void> {
  const bootstrapUrl = process.env.YY_E2E_URL;
  if (!bootstrapUrl || process.env.YY_E2E_MOCK === "1") return;

  const browser = await chromium.launch();
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(bootstrapUrl, { waitUntil: "domcontentloaded" });
    // Keep this selector tied to the semantic landmark, not to generated or localized text.
    await page.locator('nav[aria-label="工作台模式"]').waitFor({ state: "visible" });
    await context.storageState({ path: storageState });
  } finally {
    await browser.close();
  }
}
