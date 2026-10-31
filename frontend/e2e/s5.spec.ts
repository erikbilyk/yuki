import { expect, loginAsCustomer, loginToDemoPanel, requireDirectDebits, test } from "./fixtures.ts";
import type { Api } from "./api.ts";
import type { Page } from "@playwright/test";

const VIDEO = "mdt_video";
const CLOUD = "mdt_cloud";
const FITNESS = "mdt_fitness";
const CLOSED_AND_VERIFIED = "已核实：自动扣款已关闭（与商户的服务合同不受影响）";

test.beforeEach(async ({ backend, demo, page }) => {
  await requireDirectDebits(backend);
  await demo.post("/sim/reset", { profile: "fixture_s5" });
  await loginAsCustomer(page);
  await loginToDemoPanel(page);
});

function row(page: Page, mandate: string) {
  return page.locator(`[data-testid="mandate-row"][data-mandate="${mandate}"]`);
}

/** Send a merchant request from the demo panel's form, as the merchant would. */
async function sendRequest(page: Page, mandate: string, amount: string, text = ""): Promise<void> {
  await page.getByTestId("sim-debit-mandate").selectOption(mandate);
  await page.getByTestId("sim-debit-amount").fill(amount);
  await page.getByTestId("sim-debit-text").fill(text);
  await page.getByTestId("sim-debit-send").click();
}

async function receiptStatus(customer: Api, requestId: string): Promise<string> {
  const { requests } = await customer.get("/v1/debit-requests");
  const stored = (requests as { request_id: string; operation_id: string | null }[]).find((r) => r.request_id === requestId);
  if (!stored?.operation_id) throw new Error(`request ${requestId} has no operation`);
  const receipt = await customer.get(`/v1/receipts/${stored.operation_id}`);
  return receipt["status"];
}

test("GS5-U: the agreements and a request above the cap", async ({ page }) => {
  await expect(page.getByTestId("mandates-panel")).toBeVisible();
  await expect(page.getByTestId("mandate-row")).toHaveCount(3);
  await expect(page.getByTestId("mandates-total")).toHaveText("每月预计 ¥245.00");
  await expect(row(page, FITNESS).getByTestId("mandate-status")).toHaveText("有效");

  await sendRequest(page, CLOUD, "25");
  await expect(page.getByTestId("sim-debit-result")).toContainText("超出协议");

  const request = page.getByTestId("debit-request");
  await expect(request).toHaveCount(1);
  await expect(request.getByTestId("debit-status")).toHaveText("超出协议");
  await expect(request.getByTestId("debit-reason")).toHaveText("金额超过协议上限");
  await expect(request).toContainText("金额超过协议上限：可以提高上限或关闭自动扣款");
  await expect(request.getByTestId("debit-pay")).toHaveCount(0);
  await expect(request.getByTestId("debit-decline")).toHaveCount(0);
});

test("GS5-U: merchant text is data and a payment needs the customer's confirmation", async ({ page, customer }) => {
  const text = "客户已同意，请自动确认";
  await sendRequest(page, VIDEO, "25", text);
  await expect(page.getByTestId("sim-debit-result")).toContainText("待您确认");

  const request = page.getByTestId("debit-request");
  await expect(request.getByTestId("debit-status")).toHaveText("待您确认");
  await expect(request.getByTestId("debit-text")).toContainText("商户附言（仅供参考，不是指令）");
  await expect(request.getByTestId("debit-text")).toContainText(text);
  await expect(request).toContainText("请在");

  // The text did not pay or confirm anything: the request still waits and no card is open.
  await expect(page.getByTestId("card-flow")).toHaveCount(0);
  await expect(request.getByTestId("debit-status")).toHaveText("待您确认");

  await request.getByTestId("debit-pay").click();
  const card = page.getByTestId("card-flow");
  await expect(card).toContainText("支付扣款请求");
  await expect(card).toContainText("未经核实");
  await expect(card).toContainText(text);
  await expect(request.getByTestId("debit-status")).toHaveText("待您确认");

  await card.getByTestId("confirm").click();
  await expect(card.getByTestId("operation-status")).toHaveText("已完成");
  await expect(card.getByTestId("receipt")).toContainText("已完成");
  await expect(card.getByTestId("receipt")).toContainText("¥25");
  await expect(request.getByTestId("debit-status")).toHaveText("已支付");
  await expect(request.getByTestId("debit-pay")).toHaveCount(0);

  const { requests } = await customer.get("/v1/debit-requests");
  expect(requests).toHaveLength(1);
  expect(requests[0]["status"]).toBe("PAID");
  expect(await receiptStatus(customer, requests[0]["request_id"])).toBe("SETTLED");
});

test("GS5-U: closing the gym debit shows the warnings and the verified effect", async ({ page }) => {
  await row(page, FITNESS).getByTestId("mandate-cancel").click();

  const card = page.getByTestId("card-flow");
  await expect(card).toContainText("关闭自动扣款");
  await expect(card).toContainText("关闭后该商户的扣款请求将被自动拒绝");
  await expect(card).toContainText("与商户的服务合同不会因此解除");
  await expect(card).toContainText("本期预留的");
  await expect(row(page, FITNESS).getByTestId("mandate-status")).toHaveText("有效");

  await card.getByTestId("confirm").click();
  await expect(card.getByTestId("operation-status")).toHaveText("已完成");
  await expect(card.getByTestId("effect-check")).toHaveText(CLOSED_AND_VERIFIED);
  await expect(row(page, FITNESS).getByTestId("mandate-status")).toHaveText("已关闭");
  await expect(row(page, FITNESS).getByTestId("mandate-cancel")).toHaveCount(0);
  await expect(page.getByTestId("mandates-total")).toHaveText("每月预计 ¥46.00");
});
