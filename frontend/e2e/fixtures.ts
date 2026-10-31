import { existsSync, mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { expect, test as base } from "@playwright/test";
import type { Locator, Page } from "@playwright/test";
import { Api, CUSTOMER, DEMO } from "./api.ts";
import { Backend } from "./backend.ts";

const DIST = path.resolve(import.meta.dirname, "..", "dist", "index.html");

interface Fixtures {
  backend: Backend;
  /** Setup calls as the customer and as the demo operator; they stand in for the chat turn and the clock. */
  customer: Api;
  demo: Api;
}

export const test = base.extend<Fixtures>({
  backend: async ({}, use) => {
    if (!existsSync(DIST)) throw new Error("frontend/dist is missing: run `npm run build` first");
    const workdir = mkdtempSync(path.join(os.tmpdir(), "wg-e2e-"));
    const backend = new Backend(workdir);
    await backend.start();
    await use(backend);
    await backend.stop();
    rmSync(workdir, { recursive: true, force: true });
  },
  customer: async ({ backend }, use) => {
    const api = new Api(backend.url);
    await api.login(CUSTOMER.handle, CUSTOMER.password);
    await use(api);
  },
  demo: async ({ backend }, use) => {
    const api = new Api(backend.url);
    await api.login(DEMO.handle, DEMO.password);
    await use(api);
  },
  baseURL: async ({ backend }, use) => {
    await use(backend.url);
  },
});

export { expect };

export async function loginAsCustomer(page: Page): Promise<void> {
  await page.goto("/");
  const form = page.getByTestId("login-form");
  await form.getByLabel("账号").fill(CUSTOMER.handle);
  await form.getByLabel("密码").fill(CUSTOMER.password);
  await form.getByRole("button", { name: "登录" }).click();
  await expect(page.getByTestId("plan-panel")).toBeVisible();
}

/** Log the demo role in through the demo panel; it keeps its own cookie beside the customer's. */
export async function loginToDemoPanel(page: Page): Promise<void> {
  const form = page.getByTestId("demo-login");
  await form.getByLabel("演示账号").fill(DEMO.handle);
  await form.getByLabel("密码").fill(DEMO.password);
  await form.getByRole("button", { name: "登录" }).click();
  await expect(page.getByTestId("sim-clock-value")).toHaveText(/^2026-\d{2}-\d{2} \d{2}:\d{2}$/);
}

export function node(page: Page, kind: string, hasText?: string): Locator {
  const locator = page.locator(`[data-testid="plan-node"][data-node-kind="${kind}"]`);
  return hasText ? locator.filter({ hasText }) : locator;
}

export const RENT = "¥3,400";
export const INSURANCE = "¥5,000";

/** Press 获取验证码 in a node, read the code from the simulated SMS panel and enter it. */
export async function enterCode(page: Page, step: Locator): Promise<void> {
  await expect(step.getByTestId("node-phrase")).toHaveText("等待您操作：需要验证码");
  await step.getByRole("button", { name: "获取验证码" }).click();
  const sms = page.getByTestId("sms-code");
  await expect(sms).toHaveCount(1);
  const code = (await sms.textContent()) ?? "";
  expect(code).toMatch(/^[0-9]{6}$/);
  await step.getByTestId("code-input").fill(code);
  await step.getByRole("button", { name: "验证并执行" }).click();
}

export async function expectCash(page: Page, headroom: string, protectedFunds: string): Promise<void> {
  await expect(page.getByTestId("field-headroom")).toHaveText(headroom);
  await expect(page.getByTestId("field-protected")).toHaveText(protectedFunds);
}

export async function advanceToNextDue(page: Page, clock: string): Promise<void> {
  await page.getByTestId("advance-next").click();
  await expect(page.getByTestId("sim-clock-value")).toHaveText(clock);
}

/** Skip the test unless the backend lists the direct-debit feature in /healthz. */
export async function requireDirectDebits(backend: Backend): Promise<void> {
  const response = await fetch(`${backend.url}/healthz`);
  const health = (await response.json()) as { features?: string[] };
  test.skip(!health.features?.includes("s5"), "the backend does not list the s5 feature");
}
