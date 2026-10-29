import { expect, expectCash, loginAsCustomer, loginToDemoPanel, node, test } from "./fixtures.ts";

test("T67: after a real restart the purchase waits for the customer", async ({ page, backend, customer, demo }) => {
  await customer.planAtDay3(demo);
  await loginAsCustomer(page);
  await loginToDemoPanel(page);

  const purchase = node(page, "PURCHASE");
  await expect(purchase.getByTestId("node-phrase")).toHaveText("等待您操作：需要验证码");
  await expectCash(page, "¥5,985", "¥13,600");
  await expect(page.getByTestId("sim-clock-value")).toHaveText("2026-11-05 09:00");

  await page.getByTestId("crash").click();
  await expect(page.getByTestId("reconnect-banner")).toContainText("服务已中断");
  await expect(page.getByTestId("reconnect-banner")).toContainText("服务已恢复");
  expect(backend.starts).toBe(2);

  // Nothing ran by itself and no code was issued; the same fields come back from the database.
  await expect(purchase.getByTestId("node-phrase")).toHaveText("等待您操作：需要验证码");
  await expect(purchase.getByTestId("node-receipt")).toHaveCount(0);
  await expect(page.getByTestId("sms-code")).toHaveCount(0);
  await expectCash(page, "¥5,985", "¥13,600");
  await expect(page.getByTestId("sim-clock-value")).toHaveText("2026-11-05 09:00");
});
