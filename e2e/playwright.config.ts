import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import os from "node:os";

const bootstrapUrl = process.env.YY_E2E_URL;
const baseURL = process.env.YY_E2E_BASE_URL
  || (bootstrapUrl ? new URL(bootstrapUrl).origin : "http://127.0.0.1:8765");
const storageState = path.join(os.tmpdir(), "yyagent-playwright-state.json");

export default defineConfig({
  testDir: ".",
  globalSetup: path.resolve("global-setup.ts"),
  timeout: 30_000,
  expect: { timeout: 8_000 },
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? [["html", { open: "never" }], ["line"]] : "line",
  use: {
    baseURL,
    storageState: bootstrapUrl ? storageState : undefined,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});

export { storageState };
