import { advanceToNextDue, enterCode, expect, expectCash, INSURANCE, loginAsCustomer, loginToDemoPanel, node, RENT, test } from "./fixtures.ts";

test("T60: the base path from T+0 to T+7 in the browser", async ({ page, customer }) => {
  // The customer's request is a chat turn that drafts the plan; it is replaced by the same calls over HTTP.
  await customer.composePlan();

  await loginAsCustomer(page);
  await expectCash(page, "¥2,000", "¥16,000");
  await loginToDemoPanel(page);
  await expect(page.getByTestId("sim-clock")).toContainText("模拟时间");
  await expect(page.getByTestId("sim-clock-value")).toHaveText("2026-11-02 09:00");

  const plan = page.getByTestId("plan");
  await expect(page.getByTestId("plan-status")).toHaveText("待您确认");
  await expect(page.getByTestId("plan-node")).toHaveCount(6);
  await page.getByTestId("open-plan-card").click();
  const card = page.getByTestId("card-flow");
  await expect(card).toContainText("需确认");
  await card.getByTestId("confirm").click();

  // T+0: the reserve rises with the approval; the redemption waits for its code.
  await expect(page.getByTestId("plan-status")).toHaveText("执行中");
  await expectCash(page, "¥1,000", "¥17,000");
  await expect(node(page, "DESIGNATE_RESERVE").getByTestId("node-phrase")).toHaveText("已完成");
  await expect(node(page, "PAY_BILL", RENT).getByTestId("node-phrase")).toContainText("计划于 2026-11-04 09:00 执行");
  await expect(node(page, "PURCHASE").getByTestId("node-phrase")).toHaveText("等待前一步完成后执行");
  await enterCode(page, node(page, "REDEEM"));
  await expect(node(page, "REDEEM").getByTestId("node-phrase")).toHaveText("已提交，等待结算");

  // T+2: the rent.
  await advanceToNextDue(page, "2026-11-04 09:00");
  await enterCode(page, node(page, "PAY_BILL", RENT));
  const rent = node(page, "PAY_BILL", RENT);
  await expect(rent.getByTestId("node-phrase")).toHaveText("已完成");
  await rent.getByTestId("node-receipt").click();
  await expect(rent.getByTestId("node-receipt-lines")).toContainText("¥3,400");
  await expectCash(page, "¥1,000", "¥13,600");

  // T+3: the redemption settles, the purchase waits for its code.
  await advanceToNextDue(page, "2026-11-05 09:00");
  await expect(node(page, "REDEEM").getByTestId("node-phrase")).toHaveText("已完成");
  await expectCash(page, "¥5,985", "¥13,600");
  await enterCode(page, node(page, "PURCHASE"));
  await expect(node(page, "PURCHASE").getByTestId("node-phrase")).toHaveText("已完成");
  await expectCash(page, "¥1,000", "¥13,600");

  // T+5: the checkpoint.
  await advanceToNextDue(page, "2026-11-07 09:00");
  await expect(page.getByTestId("plan-checkpoint")).toContainText("已达标");
  await expect(page.getByTestId("plan-checkpoint")).toContainText("39.5089%");
  await expect(page.getByTestId("plan-status")).toHaveText("执行中");

  // T+7: the insurance.
  await advanceToNextDue(page, "2026-11-09 09:00");
  await enterCode(page, node(page, "PAY_BILL", INSURANCE));
  await expect(node(page, "PAY_BILL", INSURANCE).getByTestId("node-phrase")).toHaveText("已完成");
  await expect(page.getByTestId("plan-status")).toHaveText("已完成");
  await expectCash(page, "¥1,000", "¥8,600");
  await expect(plan.getByTestId("plan-node").getByTestId("node-phrase")).toHaveText(Array(6).fill("已完成"));

  // Audit mode lists the steps in time order and the revision with its original approval.
  await page.getByTestId("audit-toggle").click();
  await expect(page.getByTestId("audit-revision")).toContainText("初始确认");
  const submitted = page.locator('[data-testid="audit-line"][data-event="operation_submitted"]');
  const settled = page.locator('[data-testid="audit-line"][data-event="operation_settled"]');
  await expect(submitted).toHaveCount(4);
  await expect(settled).toHaveCount(4);
  await expect(page.locator('[data-testid="audit-line"][data-event="approval_created"]').first()).toBeVisible();
});
