import { expect, INSURANCE, loginAsCustomer, loginToDemoPanel, node, RENT, test } from "./fixtures.ts";

test("T70: stopping the plan while the purchase waits for its code", async ({ page, customer, demo }) => {
  await customer.planAtDay3(demo);
  await loginAsCustomer(page);
  await loginToDemoPanel(page);

  const purchase = node(page, "PURCHASE");
  await purchase.getByRole("button", { name: "获取验证码" }).click();
  await expect(purchase.getByTestId("code-input")).toBeVisible();

  await page.getByTestId("plan-stop").click();
  await expect(page.getByTestId("plan-stop-confirm")).toContainText("已提交的步骤继续结算");
  await page.getByTestId("plan-stop-yes").click();

  await expect(page.getByTestId("plan-status")).toHaveText("部分完成");
  await expect(purchase.getByTestId("node-phrase")).toHaveText("已取消");
  await expect(node(page, "PAY_BILL", INSURANCE).getByTestId("node-phrase")).toHaveText("已取消");
  await expect(node(page, "REDEEM").getByTestId("node-phrase")).toHaveText("已完成");
  await expect(node(page, "PAY_BILL", RENT).getByTestId("node-phrase")).toHaveText("已完成");
  await expect(node(page, "DESIGNATE_RESERVE").getByTestId("node-phrase")).toHaveText("已完成");
  await expect(page.getByTestId("code-input")).toHaveCount(0);

  // Without a new decision the insurance is not paid, also after the clock passes its date.
  await page.getByTestId("advance-day-input").fill("7");
  await page.getByTestId("advance-day").click();
  await expect(page.getByTestId("sim-clock-value")).toHaveText("2026-11-09 09:00");
  await expect(node(page, "PAY_BILL", INSURANCE).getByTestId("node-phrase")).toHaveText("已取消");
  await expect(page.getByTestId("plan-stop")).toHaveCount(0);
});
