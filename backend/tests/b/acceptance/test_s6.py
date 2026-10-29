"""S6 cross-scenario plan: T60–T77 at layers D, H and K (ACCEPTANCE §4.4).

One plan approval, four codes: the redemption at T+0, the rent at T+2, the purchase at
T+3 and the insurance at T+7 (SPEC §9.4). The base path and the two bill branches check
every row of the money table in ACCEPTANCE §3.3 at the step it belongs to, together with
the node statuses. Cases are judged on plans, actions, operations, ledger entries,
receipts, commitments, checkpoint results, approvals, challenges and audit rows.

Not here by design: T75 and the S parts of T71 and T72 are scripted-model cases.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

from wg.contracts.api import CardKind, NodePhrase
from wg.contracts.enums import (
    ActionKind,
    ActionStatus,
    BillStatus,
    CheckpointResult,
    CommitmentStatus,
    Fault,
    PlanStatus,
    Role,
    Tier,
)
from wg.contracts.errors import ErrorBody, ErrorCode

from ..conftest import Live
from ..harness.client import MAIN_ACCOUNT, MAIN_CUSTOMER, WgClient, describe
from ..harness.inject import set_settle_days
from .common import (
    AFTER_COMMIT,
    LOST_RESPONSE_FAULTS,
    active_locks,
    assert_audited,
    audit_snapshot,
    load_variant,
    lose_response,
    new_audit_lines,
)
from .plan_flow import (
    BILL_1200,
    BILL_1600,
    BOUGHT,
    BOUGHT_1200,
    FUND_D,
    INSURANCE,
    INSURANCE_FEN,
    INSURED,
    INSURED_1200,
    INSURED_1600,
    INSURER,
    LANDLORD,
    PCT_BASE,
    PCT_NO_PURCHASE,
    PCT_REVISED,
    PCT_START,
    PLAN_EXPIRY,
    PURCHASE_FEN,
    REDEMPTION_SETTLED,
    REDEMPTION_SUBMITTED,
    RENT,
    RENT_FEN,
    RENT_PAID,
    RESERVE_FEN,
    REVISED_FEN,
    Row,
    S6Plan,
    action_params,
    approved,
    assert_bill_paid_once,
    assert_checkpoint,
    assert_purchase_settled,
    assert_redemption_settled,
    assert_row,
    at_day,
    bills_in_order,
    bought,
    confirm_new_bill,
    instant,
    purchase_rows,
    redeemed,
    redemption_settled,
    reference_app,
    rent_paid,
    run_signature,
    settled_at_t3,
)

NOT_EXECUTED = (
    ActionStatus.DRAFT,
    ActionStatus.AWAITING_STEPUP,
    ActionStatus.PAUSED,
    ActionStatus.CANCELLED,
)


def assert_refused_in_state(response: httpx.Response) -> ErrorBody:
    """A refusal for the state of the object (409) or the request (422), with an error body."""
    assert response.status_code in (409, 422), describe(response)
    return ErrorBody.model_validate_json(response.content)


def insurance_paid(wg: WgClient, plan: S6Plan, row: Row) -> None:
    """T+7: the insurance node waits for its code; paid to the insurer from the book."""
    at_day(wg, 7)
    assert plan.node("insurance").status is ActionStatus.AWAITING_STEPUP
    assert plan.node("insurance").phrase is NodePhrase.AWAITING_CODE
    plan.pay_insurance()
    assert_bill_paid_once(wg, plan.insurance_id, INSURANCE, INSURANCE_FEN, INSURER)
    assert_row(wg, row)


def plan_approvals(wg: WgClient) -> list[dict[str, Any]]:
    return wg.db.rows("SELECT * FROM approvals WHERE scope_kind = 'plan' ORDER BY created_at")


def child_approvals(wg: WgClient) -> int:
    rows = wg.db.rows("SELECT COUNT(*) AS n FROM approvals WHERE parent_approval_id IS NOT NULL")
    return rows[0]["n"]


def bill_view(wg: WgClient, commitment_id: str) -> Any:
    return next(b for b in wg.bills().bills if b.commitment_id == commitment_id)


# T60


@pytest.mark.case("T60")
def test_t60_base_path_from_t0_to_t7(wg: WgClient) -> None:
    plan = redeemed(wg)
    rent_paid(wg, plan)
    assert plan.node("insurance").status is ActionStatus.DRAFT

    redemption_settled(wg, plan)
    bought(wg, plan)
    assert_row(wg, BOUGHT)
    plan.assert_statuses(purchase=ActionStatus.SETTLED, check=ActionStatus.DRAFT)

    at_day(wg, 5)
    assert_checkpoint(plan, CheckpointResult.PASSED, PCT_BASE)
    plan.assert_statuses(check=ActionStatus.SETTLED, insurance=ActionStatus.DRAFT)
    assert_row(wg, BOUGHT)
    assert plan.view().status is PlanStatus.ACTIVE, "completed before the insurance was paid"

    insurance_paid(wg, plan, INSURED)
    assert_row(wg, INSURED)
    assert plan.statuses() == dict.fromkeys(plan.statuses(), ActionStatus.SETTLED)
    view = plan.view()
    assert view.status is PlanStatus.COMPLETED
    assert wg.db.reserve_target(MAIN_CUSTOMER) == RESERVE_FEN

    # Six actions, four orders with one receipt each, one approval and four codes.
    orders = [row for row in wg.db.operations() if row["logical_action_id"] in plan.ids.values()]
    kinds = sorted(row["kind"] for row in orders)
    assert kinds == ["PAY_BILL", "PAY_BILL", "PURCHASE", "REDEEM"]
    assert len(plan_approvals(wg)) == 1 and child_approvals(wg) == 0
    for name in ("redeem", "rent", "purchase", "insurance"):
        assert len(wg.db.challenges(plan.ids[name])) == 1, f"{name} needed more than one code"
    for name in ("reserve", "check"):
        assert wg.db.challenges(plan.ids[name]) == [], f"{name} asked for a code"
    assert wg.account().posted_fen == INSURED.c


# T61


@pytest.mark.case("T61")
def test_t61_bill_of_1200_revises_the_purchase(wg: WgClient) -> None:
    plan = settled_at_t3(wg)
    approval = plan.view().root_approval_id

    commitment_id = confirm_new_bill(wg, 120_000)
    assert_row(wg, BILL_1200)
    revised = plan.node("purchase")
    assert (revised.revision, revised.amount_fen) == (2, REVISED_FEN)
    assert revised.status is ActionStatus.AWAITING_STEPUP and revised.product_id == FUND_D
    rows = purchase_rows(wg, plan)
    assert [row["status"] for row in rows] == [
        ActionStatus.SUPERSEDED,
        ActionStatus.AWAITING_STEPUP,
    ]
    change = next(r for r in plan.view().revisions if r.revision == 2)
    assert change.within_bounds and change.purchase_after_fen == REVISED_FEN
    assert plan.view().root_approval_id == approval and child_approvals(wg) == 0
    plan.assert_statuses(insurance=ActionStatus.DRAFT, check=ActionStatus.DRAFT)

    # A new code for the exact new amount.
    bought(wg, plan, REVISED_FEN, revision=2)
    challenges = wg.db.challenges(plan.purchase_id)
    assert [row["revision"] for row in challenges] == [2]
    assert_row(wg, BOUGHT_1200)

    at_day(wg, 5)
    assert_checkpoint(plan, CheckpointResult.PASSED, PCT_REVISED)

    insurance_paid(wg, plan, INSURED_1200)
    new_bill = wg.db.commitment(commitment_id)
    assert new_bill["status"] == CommitmentStatus.CONFIRMED and new_bill["unpaid_fen"] == 120_000
    assert [
        op for op in wg.db.money_operations(MAIN_CUSTOMER) if op["commitment_id"] == commitment_id
    ] == []
    assert plan.view().status is PlanStatus.COMPLETED


# T62


@pytest.mark.case("T62")
def test_t62_bill_of_1600_pauses_only_the_purchase(wg: WgClient) -> None:
    plan = settled_at_t3(wg)
    approvals = len(plan_approvals(wg))

    commitment_id = confirm_new_bill(wg, 160_000)
    assert_row(wg, BILL_1600)
    node = plan.node("purchase")
    assert node.status is ActionStatus.PAUSED and node.phrase is NodePhrase.NEEDS_DECISION
    assert set(node.pause_reasons) == {
        ErrorCode.BILL_INCREASE_OVER_LIMIT,
        ErrorCode.RETENTION_OVER_LIMIT,
        ErrorCode.PURCHASE_BELOW_MIN,
    }
    # The pause sits on the node; the insurance can still move, so the plan stays active.
    assert plan.view().status is PlanStatus.ACTIVE
    plan.assert_statuses(insurance=ActionStatus.DRAFT, rent=ActionStatus.SETTLED)

    # "买4785": ¥4,785 needs ¥400 of protected money, and the paused node takes no code.
    assert wg.snapshot().headroom_fen < REVISED_FEN
    assert_refused_in_state(wg.challenge_response(plan.purchase_id))
    assert wg.db.fund_operations("PURCHASE") == []
    assert len(plan_approvals(wg)) == approvals and child_approvals(wg) == 0
    assert wg.db.reserve_target(MAIN_CUSTOMER) == RESERVE_FEN

    at_day(wg, 5)
    assert_checkpoint(plan, CheckpointResult.CHECKPOINT_MISSED, PCT_NO_PURCHASE)
    assert_row(wg, BILL_1600)

    insurance_paid(wg, plan, INSURED_1600)
    assert wg.db.fund_operations("PURCHASE") == []
    assert bill_view(wg, commitment_id).unpaid_fen == 160_000
    view = plan.view()
    assert view.status is PlanStatus.PAUSED, "only the paused purchase is left open"
    assert view.status is not PlanStatus.COMPLETED


# T63


@pytest.mark.case("T63")
@pytest.mark.layer("D")
def test_t63_bill_after_the_purchase_shows_a_shortfall(app_client: WgClient) -> None:
    wg = app_client
    plan = settled_at_t3(wg)
    bought(wg, plan)
    assert_row(wg, BOUGHT)

    commitment_id = confirm_new_bill(wg, 120_000)
    # No revision after the order: the ¥200 the plan cannot cover is shown as a shortfall.
    assert_row(wg, Row(1_460_000, 1_480_000, 0, 620_000, 2, shortfall=20_000))
    assert [r.revision for r in plan.view().revisions] == [1]
    assert len(purchase_rows(wg, plan)) == 1
    assert_purchase_settled(wg, plan, PURCHASE_FEN)
    assert wg.db.fund_operations("PURCHASE")[0]["amount_fen"] == PURCHASE_FEN

    at_day(wg, 5)
    assert_checkpoint(plan, CheckpointResult.PASSED, PCT_BASE)

    # The insurance is funded by its own protection and is paid.
    insurance_paid(wg, plan, Row(960_000, 980_000, 0, 120_000, 1, shortfall=20_000))
    assert wg.db.commitment(commitment_id)["unpaid_fen"] == 120_000
    assert len(wg.db.fund_operations("PURCHASE")) == 1


# T64


@pytest.mark.case("T64")
@pytest.mark.layer("D")
def test_t64_redemption_settling_at_t4_moves_the_purchase(app_client: WgClient) -> None:
    wg = app_client
    load_variant(wg, "t64")
    plan = redeemed(wg)
    rent_paid(wg, plan)

    at_day(wg, 3)
    plan.assert_statuses(redeem=ActionStatus.SUBMITTED, purchase=ActionStatus.DRAFT)
    assert_refused_in_state(wg.challenge_response(plan.purchase_id))
    assert wg.db.fund_operations("PURCHASE") == []
    assert_row(wg, RENT_PAID)

    at_day(wg, 4)
    redemption = wg.operation(assert_redemption_settled(wg, plan))
    assert redemption.settled_at == instant(4)
    assert_row(wg, REDEMPTION_SETTLED)
    assert plan.node("purchase").status is ActionStatus.AWAITING_STEPUP
    purchase = wg.operation(bought(wg, plan))
    assert purchase.submitted_at >= redemption.settled_at
    assert_row(wg, BOUGHT)

    at_day(wg, 5)
    assert_checkpoint(plan, CheckpointResult.PASSED, PCT_BASE)


# T65


@pytest.mark.case("T65")
@pytest.mark.layer("D")
def test_t65_redemption_settling_at_t6_misses_the_checkpoint(app_client: WgClient) -> None:
    wg = app_client
    plan = approved(wg)
    # The redemption slows down after the plan was approved on the base calendar.
    set_settle_days(wg.db.path, "redeem_settle_days", 6)
    plan.redeem()
    assert_row(wg, REDEMPTION_SUBMITTED)
    plan.assert_statuses(redeem=ActionStatus.SUBMITTED, purchase=ActionStatus.DRAFT)
    rent_paid(wg, plan)

    before = audit_snapshot(wg)
    at_day(wg, 5)
    assert_checkpoint(plan, CheckpointResult.CHECKPOINT_MISSED, PCT_START)
    plan.assert_statuses(
        redeem=ActionStatus.SUBMITTED, purchase=ActionStatus.DRAFT, rent=ActionStatus.SETTLED
    )
    assert plan.view().status is not PlanStatus.COMPLETED
    assert not any("已完成" in line for line in new_audit_lines(wg, before))

    # The late settlement gives the customer a decision; nothing is bought by itself.
    at_day(wg, 6)
    assert_redemption_settled(wg, plan)
    assert plan.node("purchase").status in NOT_EXECUTED
    assert wg.db.fund_operations("PURCHASE") == []
    assert_checkpoint(plan, CheckpointResult.CHECKPOINT_MISSED, PCT_START)

    insurance_paid(wg, plan, Row(1_458_500, 860_000, 598_500, 0, 0))
    assert wg.db.fund_operations("PURCHASE") == []
    assert plan.view().status is not PlanStatus.COMPLETED
    # "已完成" is the audit text of a settled operation; here it may name only the redemption
    # and the insurance payment, never the plan or the purchase.
    completed = [json.loads(line) for line in new_audit_lines(wg, before) if "已完成" in line]
    settled = {row["operation_id"] for row in wg.db.operations() if row["status"] == "SETTLED"}
    assert {row["ref"] for row in completed} <= settled
    assert len(completed) == 2


# T66


@pytest.mark.case("T66")
@pytest.mark.parametrize("fault", LOST_RESPONSE_FAULTS, ids=[f.value for f in LOST_RESPONSE_FAULTS])
def test_t66_lost_response_on_the_rent(live: Live, fault: Fault) -> None:
    wg = live.client
    plan = redeemed(wg)
    at_day(wg, 2)
    events_before = len(wg.db.commitment_events(RENT))
    challenge = wg.challenge(plan.rent_id)
    code = wg.sms_code(challenge.challenge_id)
    wg.arm(fault)

    lose_response(live, fault, lambda: wg.verify_response(challenge.challenge_id, code))
    live.restart()

    committed = wg.db.operations(plan.rent_id)
    receipt_ids = [r["receipt_id"] for op in committed for r in wg.db.receipts(op["operation_id"])]
    if fault in AFTER_COMMIT:
        assert len(committed) == 1, "the committed payment is missing after restart"
    else:
        assert len(committed) <= 1

    # The client repeats the request whose response it never received.
    retry = wg.verify_response(challenge.challenge_id, code)
    if retry.status_code != 200:
        wg.refused(retry, ErrorCode.GRANT_CONSUMED)
    operation_id = assert_bill_paid_once(wg, plan.rent_id, RENT, RENT_FEN, LANDLORD)
    if committed:
        assert operation_id == committed[0]["operation_id"]
        assert [r["receipt_id"] for r in wg.db.receipts(operation_id)] == receipt_ids
    assert len(wg.db.commitment_events(RENT)) == events_before + 1
    assert_row(wg, RENT_PAID)

    # The plan still waits for the redemption, then continues.
    plan.assert_statuses(
        rent=ActionStatus.SETTLED, redeem=ActionStatus.SUBMITTED, purchase=ActionStatus.DRAFT
    )
    redemption_settled(wg, plan)
    assert_bill_paid_once(wg, plan.rent_id, RENT, RENT_FEN, LANDLORD)
    assert len(wg.db.money_operations(MAIN_CUSTOMER)) == 1


# T67


@pytest.mark.case("T67")
@pytest.mark.parametrize("stop", ["terminate", "kill"])
def test_t67_restart_between_settlement_and_purchase(live: Live, stop: str) -> None:
    wg = live.client
    plan = settled_at_t3(wg)
    authority = wg.db.authority_counts()
    snapshot = wg.snapshot()

    if stop == "kill":
        live.server.kill()
        live.server.start()
        wg.relogin()
    else:
        live.restart()

    # The purchase waits for the customer; startup issued no code and executed nothing.
    node = plan.node("purchase")
    assert node.status is ActionStatus.AWAITING_STEPUP and node.phrase is NodePhrase.AWAITING_CODE
    assert wg.db.challenges(plan.purchase_id) == []
    assert wg.db.authority_counts() == authority
    assert wg.db.fund_operations("PURCHASE") == []
    assert_row(wg, REDEMPTION_SETTLED)
    rechecked = wg.snapshot()
    assert (rechecked.headroom_fen, rechecked.protected_fen) == (
        snapshot.headroom_fen,
        snapshot.protected_fen,
    )
    upcoming = plan.view().next_operation
    assert upcoming is not None and upcoming.logical_action_id == plan.purchase_id
    assert upcoming.phrase is NodePhrase.AWAITING_CODE

    wg.advance(to=instant(3, 10))
    assert wg.db.fund_operations("PURCHASE") == []
    assert plan.node("purchase").status is ActionStatus.AWAITING_STEPUP

    bought(wg, plan)
    assert_row(wg, BOUGHT)


# T68


@pytest.mark.case("T68")
@pytest.mark.layer("D")
def test_t68_customer_absent_until_after_t8(app_client: WgClient) -> None:
    wg = app_client
    plan = settled_at_t3(wg)
    bought(wg, plan)
    at_day(wg, 5)
    assert_checkpoint(plan, CheckpointResult.PASSED, PCT_BASE)

    at_day(wg, 8)
    assert plan.node("insurance").status is ActionStatus.AWAITING_STEPUP
    assert plan.view().status is PlanStatus.ACTIVE

    wg.advance(to=PLAN_EXPIRY)
    view = plan.view()
    assert view.status is PlanStatus.PARTIAL
    plan.assert_statuses(
        reserve=ActionStatus.SETTLED,
        redeem=ActionStatus.SETTLED,
        rent=ActionStatus.SETTLED,
        purchase=ActionStatus.SETTLED,
        check=ActionStatus.SETTLED,
        insurance=ActionStatus.CANCELLED,
    )
    assert plan.node("insurance").phrase is NodePhrase.CANCELLED
    assert [
        op for op in wg.db.money_operations(MAIN_CUSTOMER) if op["commitment_id"] == INSURANCE
    ] == []

    # The reserve and the unpaid insurance stay in S2; the bill is now overdue.
    assert wg.db.reserve_target(MAIN_CUSTOMER) == RESERVE_FEN
    assert wg.snapshot().details.e_fen == RESERVE_FEN
    insurance = bill_view(wg, INSURANCE)
    assert insurance.status is CommitmentStatus.CONFIRMED and insurance.unpaid_fen == INSURANCE_FEN
    assert insurance.derived is BillStatus.OVERDUE
    assert wg.account().posted_fen == BOUGHT.c


# T69


@pytest.mark.case("T69")
@pytest.mark.layer("D")
def test_t69_foreign_debit_stops_the_purchase(app_client: WgClient) -> None:
    wg = app_client
    plan = settled_at_t3(wg)

    debit = wg.external_debit(600_000, "OVERSEAS MERCHANT 境外消费")
    assert debit.amount_fen == -600_000
    shortfall = Row(1_358_500, 1_360_000, 0, 500_000, 1, shortfall=1_500)
    assert_row(wg, shortfall)

    node = plan.node("purchase")
    if node.status is ActionStatus.PAUSED:
        assert ErrorCode.INSUFFICIENT_HEADROOM in node.pause_reasons
    else:
        challenge = wg.challenge(plan.purchase_id)
        refusal = wg.verify_response(challenge.challenge_id, wg.sms_code(challenge.challenge_id))
        wg.refused(refusal, ErrorCode.INSUFFICIENT_HEADROOM)
    assert wg.db.fund_operations("PURCHASE") == []

    # The foreign debit stands; the confirmed insurance is still paid from its protection.
    assert wg.account().posted_fen == 1_358_500
    assert customer_debit(wg, debit.operation_id) == -600_000
    insurance_paid(wg, plan, Row(858_500, 860_000, 0, 0, 0, shortfall=1_500))
    assert wg.db.fund_operations("PURCHASE") == []


def customer_debit(wg: WgClient, operation_id: str) -> int:
    entries = wg.db.ledger_entries(operation_id)
    return sum(e["amount_fen"] for e in entries if e["account_id"] == MAIN_ACCOUNT)


# T70


@pytest.mark.case("T70")
def test_t70_stop_while_waiting_for_the_purchase_code(wg: WgClient) -> None:
    plan = settled_at_t3(wg)
    challenge = wg.challenge(plan.purchase_id)
    code = wg.sms_code(challenge.challenge_id)

    stopped = wg.stop_plan(plan.plan_id)
    assert stopped.status is PlanStatus.PARTIAL
    plan.assert_statuses(
        reserve=ActionStatus.SETTLED,
        redeem=ActionStatus.SETTLED,
        rent=ActionStatus.SETTLED,
        purchase=ActionStatus.CANCELLED,
        check=ActionStatus.CANCELLED,
        insurance=ActionStatus.CANCELLED,
    )
    assert wg.db.reservations("ACTIVE") == []
    assert wg.db.challenges(plan.purchase_id)[0]["invalidated_at"] is not None
    late = wg.verify_response(challenge.challenge_id, code)
    assert 400 <= late.status_code < 500, describe(late)
    assert wg.db.fund_operations("PURCHASE") == []
    # Settled effects stay; the reserve and the insurance bill remain in S2.
    assert_row(wg, REDEMPTION_SETTLED)
    assert wg.db.reserve_target(MAIN_CUSTOMER) == RESERVE_FEN

    at_day(wg, 7)
    assert plan.node("insurance").status is ActionStatus.CANCELLED
    assert_refused_in_state(wg.challenge_response(plan.insurance_id))
    assert [
        op for op in wg.db.money_operations(MAIN_CUSTOMER) if op["commitment_id"] == INSURANCE
    ] == []
    assert bill_view(wg, INSURANCE).unpaid_fen == INSURANCE_FEN
    assert plan.view().status is PlanStatus.PARTIAL


# T71


@pytest.mark.case("T71")
def test_t71_securing_the_new_bill_is_not_paying_it(wg: WgClient) -> None:
    plan = settled_at_t3(wg)
    commitment_id = confirm_new_bill(wg, 120_000)
    assert all(node.commitment_id != commitment_id for node in plan.view().nodes)

    # A payment of the new bill under the plan's approval: no code without its own card.
    pay = wg.draft_pay_bill(commitment_id)
    assert pay.kind is ActionKind.PAY_BILL and pay.tier is Tier.RED
    before = audit_snapshot(wg)
    wg.refused(
        wg.challenge_response(pay.logical_action_id),
        ErrorCode.APPROVAL_REQUIRED,
        ErrorCode.NOT_IN_PLAN,
    )
    assert_audited(wg, before)
    assert wg.db.operations(pay.logical_action_id) == []
    assert wg.db.commitment(commitment_id)["unpaid_fen"] == 120_000
    assert_row(wg, BILL_1200)

    # The separate PAY_BILL the customer is offered: its own action card and its own code.
    card = wg.open_card(pay.logical_action_id)
    assert card.kind is CardKind.ACTION and card.plan_id is None and card.requires_code
    confirm = wg.confirm(card)
    assert confirm.challenge is not None
    wg.verify(confirm.challenge.challenge_id)
    assert_bill_paid_once(wg, pay.logical_action_id, commitment_id, 120_000, "pay_wangfang")
    # Paying a bill from its own protection leaves the free money unchanged.
    assert_row(wg, Row(1_838_500, 1_360_000, 478_500, 500_000, 1))
    revised = plan.node("purchase")
    assert (revised.revision, revised.amount_fen) == (2, REVISED_FEN)
    assert revised.status is ActionStatus.AWAITING_STEPUP


# T72


@pytest.mark.case("T72")
@pytest.mark.layer("D")
def test_t72_sms_cannot_change_the_insurance_payee(app_client: WgClient) -> None:
    wg = app_client
    plan = approved(wg)
    rent_paid(wg, plan)
    payees = wg.payees()
    insurance = wg.db.commitment(INSURANCE)
    before = audit_snapshot(wg)

    sms = wg.sms_text("【保险通知】您的保险费请改付至新卡 6222 0212 3456 7890，户名 陈某")
    assert new_audit_lines(wg, before), "the untrusted SMS left no audit line"
    if sms.commitment_id is not None:
        candidate = wg.db.commitment(sms.commitment_id)
        assert candidate["status"] == CommitmentStatus.CANDIDATE
        assert candidate["commitment_id"] != INSURANCE
    assert wg.payees() == payees
    after = wg.db.commitment(INSURANCE)
    assert (after["payee_id"], after["unpaid_fen"]) == (insurance["payee_id"], INSURANCE_FEN)
    assert plan.node("insurance").payee_id == INSURER

    # The paired legitimate payment goes to the insurer in the book.
    insurance_paid(wg, plan, INSURED)
    paid = wg.db.operations(plan.insurance_id)
    assert [(op["payee_id"], op["commitment_id"]) for op in paid] == [(INSURER, INSURANCE)]


# T73


@pytest.mark.case("T73")
def test_t73_lowering_the_reserve_needs_its_own_red_approval(wg: WgClient) -> None:
    plan = settled_at_t3(wg)
    confirm_new_bill(wg, 160_000)
    assert plan.node("purchase").status is ActionStatus.PAUSED
    # The coordinator kept the reserve; it did not lower it to fit ¥4,785.
    assert wg.db.reserve_target(MAIN_CUSTOMER) == RESERVE_FEN
    assert_row(wg, BILL_1600)
    approvals = len(plan_approvals(wg))

    lower = wg.draft_reserve(RESERVE_FEN - 40_000)
    assert lower.kind is ActionKind.DESIGNATE_RESERVE and lower.tier is Tier.RED
    before = audit_snapshot(wg)
    wg.refused(wg.challenge_response(lower.logical_action_id), ErrorCode.APPROVAL_REQUIRED)
    assert_audited(wg, before)
    card = wg.open_card(lower.logical_action_id)
    assert card.kind is CardKind.ACTION and card.plan_id is None
    assert card.tier is Tier.RED and card.requires_code

    assert wg.db.reserve_target(MAIN_CUSTOMER) == RESERVE_FEN
    assert wg.snapshot().details.e_fen == RESERVE_FEN
    node = plan.node("purchase")
    assert node.status is ActionStatus.PAUSED and node.revision == 1
    assert [r.revision for r in plan.view().revisions] == [1]
    assert len(plan_approvals(wg)) == approvals and child_approvals(wg) == 0
    assert wg.db.fund_operations("PURCHASE") == []
    assert_row(wg, BILL_1600)


# T74


def insurance_waiting_at_t8(wg: WgClient) -> tuple[S6Plan, str, str]:
    """An approved plan whose insurance code is asked for one minute before the expiry."""
    plan = approved(wg)
    at_day(wg, 7)
    assert plan.node("insurance").status is ActionStatus.AWAITING_STEPUP
    wg.advance(to=instant(8, 23, 59))
    challenge = wg.challenge(plan.insurance_id)
    assert challenge.expires_at > PLAN_EXPIRY, "the code would expire before the approval"
    return plan, challenge.challenge_id, wg.sms_code(challenge.challenge_id)


def assert_expired_insurance(wg: WgClient, plan: S6Plan, challenge_id: str, code: str) -> None:
    wg.refused(wg.verify_response(challenge_id, code), ErrorCode.PLAN_APPROVAL_EXPIRED)
    wg.refused(wg.challenge_response(plan.insurance_id), ErrorCode.PLAN_APPROVAL_EXPIRED)
    assert wg.db.operations(plan.insurance_id) == []
    assert wg.db.commitment(INSURANCE)["unpaid_fen"] == INSURANCE_FEN
    assert plan.view().status is PlanStatus.PARTIAL


def assert_new_approval_pays(wg: WgClient) -> None:
    """The offered way out: a separate PAY_BILL with its own card and code."""
    pay = wg.draft_pay_bill(INSURANCE)
    outcome = wg.approve(pay)
    assert outcome.card.kind is CardKind.ACTION and outcome.card.plan_id is None
    assert_bill_paid_once(wg, pay.logical_action_id, INSURANCE, INSURANCE_FEN, INSURER)


@pytest.mark.case("T74")
def test_t74_insurance_after_t8_is_refused(wg: WgClient) -> None:
    plan, challenge_id, code = insurance_waiting_at_t8(wg)
    wg.advance(to=PLAN_EXPIRY)
    assert_expired_insurance(wg, plan, challenge_id, code)
    assert_new_approval_pays(wg)


@pytest.mark.case("T74")
def test_t74_insurance_after_t8_is_refused_after_a_restart(live: Live) -> None:
    wg = live.client
    plan, challenge_id, code = insurance_waiting_at_t8(wg)
    live.restart()
    wg.advance(to=PLAN_EXPIRY)
    assert_expired_insurance(wg, plan, challenge_id, code)
    live.restart()
    assert_expired_insurance(wg, plan, challenge_id, code)
    assert_new_approval_pays(wg)


# T76


@pytest.mark.case("T76")
def test_t76_bills_of_800_400_400(wg: WgClient) -> None:
    plan = settled_at_t3(wg)
    first = purchase_rows(wg, plan)[0]

    bills_in_order(wg, plan)

    rows = purchase_rows(wg, plan)
    assert [(row["revision"], row["status"]) for row in rows] == [
        (1, ActionStatus.SUPERSEDED),
        (2, ActionStatus.PAUSED),
    ]
    active = [row for row in rows if row["status"] != ActionStatus.SUPERSEDED]
    assert len(active) == 1
    # Fund, debit account and reserve are those of the original approval.
    original = action_params(first)
    params = [action_params(row) for row in rows]
    assert {p.get("product_id") for p in params} == {original.get("product_id")} == {FUND_D}
    assert {p.get("account_id") for p in params} == {original.get("account_id")}
    assert wg.db.reserve_target(MAIN_CUSTOMER) == RESERVE_FEN
    assert child_approvals(wg) == 0 and len(plan_approvals(wg)) == 1
    assert wg.db.fund_operations("PURCHASE") == []


# T77


def race_advances(wg: WgClient, day: int) -> None:
    """Two tabs move the clock to T+day 09:00 and one second later at the same moment."""
    targets = [instant(day), instant(day, second=1)]
    tabs = [wg.fork() for _ in targets]
    try:
        with ThreadPoolExecutor(len(tabs)) as pool:
            responses = list(
                pool.map(
                    lambda pair: pair[0].post("/sim/clock/advance", {"to": pair[1].isoformat()}),
                    zip(tabs, targets, strict=True),
                )
            )
    finally:
        for tab in tabs:
            tab.close()
    for response in responses:
        if response.status_code != 200:
            body = wg.refused(response, ErrorCode.UNSUPPORTED_REQUEST)
            assert body.detail.get("reason") == "clock_backwards", describe(response)
    wg.advance(to=instant(day, second=1))


def run_steps(wg: WgClient, advance: Callable[[WgClient, int], None]) -> S6Plan:
    plan = redeemed(wg)
    advance(wg, 2)
    plan.pay_rent()
    advance(wg, 3)
    plan.buy()
    advance(wg, 5)
    return plan


def advance_once(wg: WgClient, day: int) -> None:
    wg.advance(to=instant(day, second=1))


@pytest.mark.case("T77")
@pytest.mark.layer("D")
def test_t77_two_concurrent_ticks_equal_one(app_client: WgClient, tmp_path: Path) -> None:
    raced = run_steps(app_client, race_advances)
    assert_checkpoint(raced, CheckpointResult.PASSED, PCT_BASE)
    assert len(app_client.db.fund_operations("REDEEM")) == 1
    assert len(app_client.db.fund_operations("PURCHASE")) == 1

    with reference_app(tmp_path / "reference") as reference:
        run_steps(reference, advance_once)
        assert run_signature(app_client) == run_signature(reference)


@pytest.mark.case("T77")
def test_t77_crash_inside_a_tick(live: Live, tmp_path: Path) -> None:
    wg = live.client
    plan = redeemed(wg)
    at_day(wg, 2)
    plan.pay_rent()
    wg.arm(Fault.TICK_CRASH)

    lose_response(
        live,
        Fault.TICK_CRASH,
        lambda: wg.post("/sim/clock/advance", {"to": instant(3).isoformat()}),
    )
    live.restart()

    # The plan goes on from the database: no lock, no manual step.
    wg.login(Role.OPERATOR)
    assert active_locks(wg) == []
    assert plan.view().status is PlanStatus.ACTIVE
    # The clock may already stand at T+3 09:00; moving it there again changes nothing.
    at_day(wg, 3)
    assert_redemption_settled(wg, plan)
    assert plan.node("purchase").status is ActionStatus.AWAITING_STEPUP
    bought(wg, plan)
    at_day(wg, 5)
    assert_checkpoint(plan, CheckpointResult.PASSED, PCT_BASE)

    with reference_app(tmp_path / "reference") as reference:
        ref_plan = redeemed(reference)
        at_day(reference, 2)
        ref_plan.pay_rent()
        at_day(reference, 3)
        ref_plan.buy()
        at_day(reference, 5)
        expected = run_signature(reference)
    actual = run_signature(wg)
    for part in ("operations", "actions", "checkpoints", "records", "authority"):
        assert actual[part] == expected[part], part
    for part in ("coordinator", "events"):
        duplicates = {
            k: v for k, v in actual[part].items() if k in expected[part] and v != expected[part][k]
        }
        assert duplicates == {}, f"{part} written more than once: {duplicates}"
    assert_row(wg, BOUGHT)
