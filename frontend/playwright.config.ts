import { defineConfig } from "@playwright/test";

// Each test starts its own backend (see e2e/fixtures.ts), so the tests run one after another.
export default defineConfig({
  testDir: "e2e",
  testMatch: "**/*.spec.ts",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 10_000 },
  outputDir: "test-results",
  reporter: [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]],
  use: {
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    viewport: { width: 1360, height: 1000 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    // Local backend only: never go through a proxy taken from the environment.
    launchOptions: { args: ["--no-proxy-server"] },
  },
});
