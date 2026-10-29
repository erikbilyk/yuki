"""Regressions R1–R7 and R9 (ACCEPTANCE §4.7): the resolved contracts, pinned.

R1 C-09 cumulative bill bounds; R2 SPEC §15.3 clock jumps; R3 C-10 acknowledgement and
receipt; R4 SPEC §12.6 UNKNOWN and reconciliation; R5 C-20 expiry boundaries; R6 C-21 daily
sum; R7 C-11/C-12 failed dependency and the checkpoint under a lock; R9 SPEC §12.8 restart.

R4's integrity variant removes the bank's record of an order with the harness's own
connection (`harness/inject.py`); the product never offers that write.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from wg.contracts.api import ChallengeView
from wg.contracts.enums import (
    ActionStatus,
    CheckpointResult,
    Fault,
    OperationStatus,
    PlanStatus,
    ReceiptStatus,
    Role,
    Tier,
)
from wg.contracts.errors import ErrorCode

from ..conftest import Live
from ..harness.client import MAIN_CUSTOMER, WgClient, describe
from ..harness.inject import delete_bank_request, rewind_event_cursors
from .common import active_locks, load_variant
from .plan_flow import (
    BOUGHT,
    DAD,
    INSURANCE,
    INSURANCE_FEN,
    INSURER,
    LANDLORD,
    MOM,
    NET_FEN,
    PCT_BASE,
    PCT_NO_PURCHASE,
    PCT_START,
    PLAN_EXPIRY,
    PURCHASE_FEN,
    REDEMPTION_SETTLED,
    RENT,
    RENT_FEN,
    RENT_PAID,
    REVISED_FEN,
    S6Plan,
    action_params,
    approved,
    assert_bill_paid_once,
    assert_checkpoint,
    assert_redemption_settled,
    assert_row,
    at_day,
    bills_in_order,
    bought,
    customer_effect,
    instant,
    purchase_rows,
    redeemed,
    redemption_settled,
    rent_paid,
    settled_at_t3,
    single_operation,
)

THREE_REASONS = {
    ErrorCode.BILL_INCREASE_OVER_LIMIT,
    ErrorCode.RETENTION_OVER_LIMIT,
    ErrorCode.PURCHASE_BELOW_MIN,
}
# What the faulty sketch would have bought after ¥1,600 of bills.
FAULTY_FEN = 458_500


# R1


def purchase_state(wg: WgClient, plan: S6Plan) -> dict[str, Any]:
    view = plan.view()
    return {
        "rows": [
            (row["revision"], row["status"], row["pause_reasons"])
            for row in purchase_rows(wg, plan)
        ],
        "revisions": [(r.revision, r.purchase_after_fen) for r in view.revisions],
        "node": plan.node("purchase").model_dump(),
        "cash": _cash(wg),
        "challenges": len(wg.db.challenges(plan.purchase_id)),
        "approvals": wg.db.authority_counts()["approvals"],
    }


def _cash(wg: WgClient) -> tuple[int, ...]:
    view = wg.snapshot()
    return (
        view.details.c_fen,
        view.protected_fen,
        view.headroom_fen,
        view.shortfall_fen,
        view.unpaid_bills_fen,
        view.unpaid_bills_count,
    )


@pytest.mark.case("R1")
@pytest.mark.layer("D")
def test_r1_bill_events_count_once_from_the_original_approval(app_client: WgClient) -> None:
    wg = app_client
    plan = settled_at_t3(wg)
    bills = bills_in_order(wg, plan)
    node = plan.node("purchase")
    assert set(node.pause_reasons) == THREE_REASONS and len(node.pause_reasons) == 3
    settled = purchase_state(wg, plan)

    # The second and third bill events are delivered again: confirmed twice, then replayed.
    for _, card in bills[1:]:
        repeat = wg.confirm_response(card)
        assert repeat.status_code == 200, describe(repeat)
    rewind_event_cursors(wg.db.path)
    wg.advance(minutes=1)

    assert purchase_state(wg, plan) == settled
    rows = purchase_rows(wg, plan)
    assert len([row for row in rows if row["status"] != ActionStatus.SUPERSEDED]) == 1
    amounts = {action_params(row).get("amount_fen") for row in rows} | {
        r.purchase_after_fen for r in plan.view().revisions
    }
    assert FAULTY_FEN not in amounts
    assert amounts <= {PURCHASE_FEN, REVISED_FEN, None}
    assert wg.db.fund_operations("PURCHASE") == []


# R2


@pytest.mark.case("R2")
@pytest.mark.layer("D")
def test_r2_one_jump_from_t3_to_t7_records_the_checkpoint_at_t5(app_client: WgClient) -> None:
    wg = app_client
    plan = settled_at_t3(wg)
    bought(wg, plan)

    wg.advance(to=instant(7))
    assert_checkpoint(plan, CheckpointResult.PASSED, PCT_BASE, at=instant(5))
    assert plan.node("insurance").status is ActionStatus.AWAITING_STEPUP
    assert_row(wg, BOUGHT)


@pytest.mark.case("R2")
@pytest.mark.layer("D")
def test_r2_delayed_redemption_and_one_jump_from_t2_to_t7(app_client: WgClient) -> None:
    wg = app_client
    load_variant(wg, "t65")
    plan = redeemed(wg)
    at_day(wg, 2)
    challenge = wg.challenge(plan.rent_id)
    code = wg.sms_code(challenge.challenge_id)

    wg.advance(to=instant(7))
    # Due instants in ascending order: the check at T+5 on the positions settled then,
    # the redemption at T+6, which never rewrites the check.
    assert_checkpoint(plan, CheckpointResult.CHECKPOINT_MISSED, PCT_START, at=instant(5))
    redemption = wg.operation(assert_redemption_settled(wg, plan))
    assert redemption.settled_at == instant(6)
    assert wg.db.fund_operations("PURCHASE") == []

    # The rent code issued before the jump has expired; a new one pays the rent once.
    wg.refused(wg.verify_response(challenge.challenge_id, code), ErrorCode.CODE_EXPIRED)
    assert wg.db.operations(plan.rent_id) == []
    assert plan.node("rent").status is ActionStatus.AWAITING_STEPUP
    plan.pay_rent()
    assert_bill_paid_once(wg, plan.rent_id, RENT, RENT_FEN, LANDLORD)
    assert_checkpoint(plan, CheckpointResult.CHECKPOINT_MISSED, PCT_START, at=instant(5))


# R3


def other_bank_transfer(wg: WgClient, amount_fen: int = 50_000) -> str:
    """A yellow transfer to a 他行 payee: accepted now, settled at the next 09:00."""
    outcome = wg.transfer(DAD, amount_fen)
    assert outcome.draft.tier is Tier.YELLOW
    operation = outcome.operation
    assert operation is not None and operation.status is OperationStatus.SUBMITTED
    return operation.operation_id


def assert_submitted_without_receipt(wg: WgClient, operation_id: str) -> None:
    assert wg.operation(operation_id).status is OperationStatus.SUBMITTED
    events = [row["event"] for row in wg.db.operation_events(operation_id)]
    assert events.count("SUBMITTED") == 1
    assert wg.db.receipts(operation_id) == []
    response = wg.get(f"/v1/receipts/{operation_id}")
    wg.refused(response, ErrorCode.NOT_FOUND)


def settled_state(wg: WgClient, operation_id: str) -> dict[str, Any]:
    return {
        "status": wg.operation(operation_id).status,
        "receipts": wg.db.receipts(operation_id),
        "ledger": wg.db.ledger_entries(operation_id),
        "events": [row["event"] for row in wg.db.operation_events(operation_id)],
        "posted": wg.account().posted_fen,
    }


@pytest.mark.case("R3")
@pytest.mark.layer("D")
def test_r3_submission_is_acknowledged_and_settlement_writes_one_receipt(
    app_client: WgClient,
) -> None:
    wg = app_client
    operation_id = other_bank_transfer(wg)
    assert_submitted_without_receipt(wg, operation_id)

    wg.advance(days=1)
    assert wg.operation(operation_id).status is OperationStatus.SETTLED
    receipt_id = _one_receipt(wg, operation_id, ReceiptStatus.SETTLED)
    assert customer_effect(wg, operation_id) == -50_000
    settled = settled_state(wg, operation_id)

    # The settlement is observed again: later clock moves, reconciliation, replayed events.
    wg.advance(days=1)
    wg.login(Role.OPERATOR)
    wg.reconcile(MAIN_CUSTOMER)
    rewind_event_cursors(wg.db.path)
    wg.advance(minutes=1)
    assert settled_state(wg, operation_id) == settled
    assert wg.receipt(operation_id).receipt_id == receipt_id


@pytest.mark.case("R3")
@pytest.mark.layer("D")
def test_r3_a_rejection_is_terminal(app_client: WgClient) -> None:
    wg = app_client
    operation_id = other_bank_transfer(wg)
    wg.arm(Fault.SETTLEMENT_REJECT)
    wg.advance(days=1)
    assert wg.operation(operation_id).status is OperationStatus.REJECTED
    _one_receipt(wg, operation_id, ReceiptStatus.REJECTED)
    assert wg.db.ledger_entries(operation_id) == []
    assert wg.db.reservations("ACTIVE") == []
    rejected = settled_state(wg, operation_id)

    wg.advance(days=1)
    wg.login(Role.OPERATOR)
    wg.reconcile(MAIN_CUSTOMER)
    assert settled_state(wg, operation_id) == rejected


@pytest.mark.case("R3")
def test_r3_restart_between_submission_and_settlement(live: Live) -> None:
    wg = live.client
    operation_id = other_bank_transfer(wg)
    live.server.kill()
    live.server.start()
    wg.relogin()
    assert_submitted_without_receipt(wg, operation_id)

    wg.advance(days=1)
    receipt_id = _one_receipt(wg, operation_id, ReceiptStatus.SETTLED)
    settled = settled_state(wg, operation_id)

    live.server.stop()
    rewind_event_cursors(live.server.db_path)
    live.server.start()
    wg.relogin()
    wg.advance(days=1)
    assert settled_state(wg, operation_id) == settled
    assert wg.receipt(operation_id).receipt_id == receipt_id


def _one_receipt(wg: WgClient, operation_id: str, status: ReceiptStatus) -> str:
    stored = wg.db.receipts(operation_id)
    assert len(stored) == 1 and stored[0]["status"] == status, stored
    return stored[0]["receipt_id"]


# R4


def unknown_after_lost_ack(wg: WgClient, plan: S6Plan, name: str) -> dict[str, Any]:
    """Enter the code while the bank's acknowledgement is lost; the effect exists."""
    challenge = wg.challenge(plan.ids[name])
    code = wg.sms_code(challenge.challenge_id)
    wg.arm(Fault.BANK_ACK_LOST)
    response = wg.verify_response(challenge.challenge_id, code)
    assert response.status_code == 202, describe(response)
    row = single_operation(wg, plan.ids[name])
    assert row["status"] == OperationStatus.UNKNOWN
    assert wg.db.action_status(plan.ids[name]) == ActionStatus.UNKNOWN
    assert len(wg.db.bank_requests(row["idempotency_key"])) == 1, "no bank record of the order"
    assert wg.db.receipts(row["operation_id"]) == []
    return row


def rent_unknown(wg: WgClient) -> tuple[S6Plan, dict[str, Any]]:
    plan = approved(wg)
    at_day(wg, 2)
    return plan, unknown_after_lost_ack(wg, plan, "rent")


def assert_rent_restored(wg: WgClient, plan: S6Plan, unknown: dict[str, Any]) -> None:
    """Reconciled by the same key: the original operation, its effect and one receipt."""
    operation_id = assert_bill_paid_once(wg, plan.rent_id, RENT, RENT_FEN, LANDLORD)
    assert operation_id == unknown["operation_id"]
    row = single_operation(wg, plan.rent_id)
    assert row["idempotency_key"] == unknown["idempotency_key"]
    assert len(wg.db.bank_requests(unknown["idempotency_key"])) == 1
    events = [e["event"] for e in wg.db.operation_events(operation_id)]
    assert "RECONCILED" in events
    assert len(wg.db.money_operations(MAIN_CUSTOMER)) == 1
    assert_row(wg, RENT_PAID)


@pytest.mark.case("R4")
@pytest.mark.layer("D")
def test_r4_lost_ack_on_the_rent_is_counted_and_reconciled(app_client: WgClient) -> None:
    wg = app_client
    plan, unknown = rent_unknown(wg)

    # The UNKNOWN ¥3,400 counts in the day's sum: ¥100 more is already red.
    probe = wg.draft_transfer(MOM, 10_000)
    assert probe.tier is Tier.RED

    wg.login(Role.OPERATOR)
    result = wg.reconcile(MAIN_CUSTOMER)
    assert result.checked >= 1 and result.resolved >= 1
    assert_rent_restored(wg, plan, unknown)


@pytest.mark.case("R4")
@pytest.mark.layer("D")
def test_r4_lost_ack_on_the_redemption_is_reconciled(app_client: WgClient) -> None:
    wg = app_client
    plan = approved(wg)
    unknown = unknown_after_lost_ack(wg, plan, "redeem")

    wg.login(Role.OPERATOR)
    wg.reconcile(MAIN_CUSTOMER)
    row = single_operation(wg, plan.redeem_id)
    assert row["operation_id"] == unknown["operation_id"]
    assert row["idempotency_key"] == unknown["idempotency_key"]
    assert row["status"] == OperationStatus.SUBMITTED
    assert plan.node("redeem").status is ActionStatus.SUBMITTED

    rent_paid(wg, plan)
    redemption_settled(wg, plan)
    assert customer_effect(wg, unknown["operation_id"]) == NET_FEN
    assert len(wg.db.fund_operations("REDEEM")) == 1


def assert_integrity_lock(wg: WgClient, unknown: dict[str, Any]) -> None:
    wg.login(Role.OPERATOR)
    locks = active_locks(wg)
    assert locks, "reconciliation without a bank record set no lock"
    failures = [f"{row['error_class']} {row['detail']}" for row in wg.db.failure_records()]
    reasons = [lock.reason for lock in locks] + failures
    assert any(ErrorCode.INTEGRITY_VIOLATION.value in text for text in reasons), reasons
    row = single_operation(wg, unknown["logical_action_id"])
    assert row["status"] == OperationStatus.UNKNOWN
    assert wg.db.receipts(row["operation_id"]) == []


@pytest.mark.case("R4")
@pytest.mark.layer("D")
def test_r4_missing_bank_record_locks_with_integrity_violation(app_client: WgClient) -> None:
    wg = app_client
    _, unknown = rent_unknown(wg)
    delete_bank_request(wg.db.path, unknown["idempotency_key"])

    wg.login(Role.OPERATOR)
    response = wg.post("/v1/operator/reconcile", {"customer_id": MAIN_CUSTOMER})
    if response.status_code != 200:
        wg.refused(response, ErrorCode.INTEGRITY_VIOLATION)
    assert_integrity_lock(wg, unknown)


@pytest.mark.case("R4")
def test_r4_restart_reconciles_the_lost_ack(live: Live) -> None:
    wg = live.client
    plan, unknown = rent_unknown(wg)
    live.restart()
    assert_rent_restored(wg, plan, unknown)


@pytest.mark.case("R4")
def test_r4_restart_without_a_bank_record_locks(live: Live) -> None:
    wg = live.client
    _, unknown = rent_unknown(wg)
    live.server.stop()
    delete_bank_request(live.server.db_path, unknown["idempotency_key"])
    live.server.start()
    wg.relogin()
    assert_integrity_lock(wg, unknown)


# R5


def insurance_code_at(wg: WgClient, plan: S6Plan, hour: int, minute: int) -> ChallengeView:
    at_day(wg, 7)
    wg.advance(to=instant(8, hour, minute))
    return wg.challenge(plan.insurance_id)


@pytest.mark.case("R5")
@pytest.mark.layer("D")
def test_r5_plan_approval_valid_at_the_last_second_of_t8(app_client: WgClient) -> None:
    wg = app_client
    plan = approved(wg)
    challenge = insurance_code_at(wg, plan, 23, 59)
    wg.advance(to=PLAN_EXPIRY - timedelta(seconds=1))
    wg.verify(challenge.challenge_id)
    assert_bill_paid_once(wg, plan.insurance_id, INSURANCE, INSURANCE_FEN, INSURER)


@pytest.mark.case("R5")
@pytest.mark.layer("D")
def test_r5_plan_approval_expired_at_t9_midnight(app_client: WgClient) -> None:
    wg = app_client
    plan = approved(wg)
    challenge = insurance_code_at(wg, plan, 23, 59)
    code = wg.sms_code(challenge.challenge_id)
    wg.advance(to=PLAN_EXPIRY)
    wg.refused(wg.verify_response(challenge.challenge_id, code), ErrorCode.PLAN_APPROVAL_EXPIRED)
    assert wg.db.operations(plan.insurance_id) == []


@pytest.mark.case("R5")
@pytest.mark.layer("D")
@pytest.mark.parametrize("late", [False, True], ids=["at_4m59s", "at_5m"])
def test_r5_code_valid_for_five_minutes(app_client: WgClient, late: bool) -> None:
    wg = app_client
    plan = approved(wg)
    challenge = wg.challenge(plan.redeem_id)
    assert challenge.expires_at == instant(0) + timedelta(minutes=5)
    code = wg.sms_code(challenge.challenge_id)

    if late:
        wg.advance(to=challenge.expires_at)
        response = wg.verify_response(challenge.challenge_id, code)
        wg.refused(response, ErrorCode.CODE_EXPIRED)
        assert wg.db.operations(plan.redeem_id) == []
    else:
        wg.advance(to=challenge.expires_at - timedelta(seconds=1))
        wg.verify(challenge.challenge_id, code)
        assert single_operation(wg, plan.redeem_id)["status"] == OperationStatus.SUBMITTED


@pytest.mark.case("R5")
def test_r5_restarts_extend_neither_code_nor_approval(live: Live) -> None:
    wg = live.client
    plan = approved(wg)
    challenge = wg.challenge(plan.redeem_id)
    code = wg.sms_code(challenge.challenge_id)
    wg.advance(minutes=4)
    live.restart()
    wg.advance(to=challenge.expires_at)
    wg.refused(wg.verify_response(challenge.challenge_id, code), ErrorCode.CODE_EXPIRED)

    insurance = insurance_code_at(wg, plan, 23, 59)
    insurance_code = wg.sms_code(insurance.challenge_id)
    live.restart()
    wg.advance(to=PLAN_EXPIRY)
    live.restart()
    response = wg.verify_response(insurance.challenge_id, insurance_code)
    wg.refused(response, ErrorCode.PLAN_APPROVAL_EXPIRED)
    assert wg.db.operations(plan.insurance_id) == []
    assert plan.view().status is PlanStatus.PARTIAL


# R6


def probe_tier(wg: WgClient, amount_fen: int) -> Tier:
    """Tier of a new transfer draft: yellow while the day's sum stays within ¥1,000."""
    return wg.draft_transfer(MOM, amount_fen).tier


@pytest.mark.case("R6")
@pytest.mark.layer("D")
def test_r6_daily_sum_counts_each_live_operation_once(app_client: WgClient) -> None:
    wg = app_client
    # A new simulated day starts before 09:00, so a 他行 rejection lands on the same day.
    wg.advance(to=instant(1, 0, 1))

    rejected = other_bank_transfer(wg, 20_000)
    wg.arm(Fault.SETTLEMENT_REJECT)
    wg.advance(to=instant(1))
    assert wg.operation(rejected).status is OperationStatus.REJECTED

    cancelled = wg.draft_transfer(MOM, 25_000)
    wg.request_approval(cancelled.logical_action_id)
    wg.cancel(cancelled.logical_action_id)
    assert wg.db.action_status(cancelled.logical_action_id) == ActionStatus.CANCELLED

    settled = wg.transfer(MOM, 10_000).operation
    assert settled is not None and settled.status is OperationStatus.SETTLED
    submitted = other_bank_transfer(wg, 15_000)
    assert [r["amount_fen"] for r in wg.db.reservations("ACTIVE")] == [15_000]

    pending = wg.draft_transfer(MOM, 20_000)
    card = wg.open_card(pending.logical_action_id)
    wg.arm(Fault.BANK_ACK_LOST)
    response = wg.confirm_response(card)
    assert response.status_code == 202, describe(response)
    assert single_operation(wg, pending.logical_action_id)["status"] == OperationStatus.UNKNOWN

    # Counted: ¥100 SETTLED + ¥150 SUBMITTED (with its hold) + ¥200 UNKNOWN = ¥450.
    assert probe_tier(wg, 55_000) is Tier.YELLOW
    assert probe_tier(wg, 55_001) is Tier.RED

    wg.login(Role.OPERATOR)
    wg.reconcile(MAIN_CUSTOMER)
    assert wg.operation(submitted).status is OperationStatus.SUBMITTED
    assert probe_tier(wg, 55_000) is Tier.YELLOW
    assert probe_tier(wg, 55_001) is Tier.RED


# R7


@pytest.mark.case("R7")
@pytest.mark.layer("D")
def test_r7_rejected_redemption_pauses_the_purchase(app_client: WgClient) -> None:
    wg = app_client
    plan = redeemed(wg)
    rent_paid(wg, plan)
    wg.arm(Fault.SETTLEMENT_REJECT)

    at_day(wg, 3)
    redemption = single_operation(wg, plan.redeem_id)
    assert redemption["status"] == OperationStatus.REJECTED
    _one_receipt(wg, redemption["operation_id"], ReceiptStatus.REJECTED)
    node = plan.node("purchase")
    assert node.status is ActionStatus.PAUSED
    assert node.pause_reasons == (ErrorCode.DEPENDENCY_FAILED,)
    assert plan.node("insurance").status is ActionStatus.DRAFT
    assert_row(wg, RENT_PAID)

    at_day(wg, 5)
    assert_checkpoint(plan, CheckpointResult.CHECKPOINT_MISSED, PCT_START)
    assert wg.db.fund_operations("PURCHASE") == []

    # The independent payment still runs.
    at_day(wg, 7)
    plan.pay_insurance()
    assert_bill_paid_once(wg, plan.insurance_id, INSURANCE, INSURANCE_FEN, INSURER)


@pytest.mark.case("R7")
@pytest.mark.layer("D")
def test_r7_checkpoint_is_recorded_under_a_lock(app_client: WgClient) -> None:
    wg = app_client
    plan = settled_at_t3(wg)
    challenge = wg.challenge(plan.purchase_id)
    code = wg.sms_code(challenge.challenge_id)
    wg.login(Role.OPERATOR)

    wg.arm(Fault.UNEXPECTED, count=3)
    for _ in range(3):
        wg.refused(wg.verify_response(challenge.challenge_id, code), ErrorCode.UNEXPECTED_FAILURE)
    assert active_locks(wg), "no lock after three unexpected failures"
    assert wg.db.fund_operations("PURCHASE") == []

    at_day(wg, 5)
    assert_checkpoint(plan, CheckpointResult.CHECKPOINT_MISSED, PCT_NO_PURCHASE)
    assert active_locks(wg)
    assert wg.db.fund_operations("PURCHASE") == []
    assert_row(wg, REDEMPTION_SETTLED)


# R9


def assert_same_after_restart(live: Live, plan: S6Plan) -> None:
    wg = live.client
    authority = wg.db.authority_counts()
    statuses = plan.statuses()
    live.restart()
    assert wg.db.authority_counts() == authority
    assert plan.statuses() == statuses


@pytest.mark.case("R9")
def test_r9_restarts_at_every_step_create_no_authority(live: Live) -> None:
    wg = live.client
    plan = approved(wg)
    assert_same_after_restart(live, plan)
    plan.redeem()
    assert_same_after_restart(live, plan)
    at_day(wg, 2)
    assert_same_after_restart(live, plan)
    plan.pay_rent()
    assert_same_after_restart(live, plan)
    at_day(wg, 3)
    assert_same_after_restart(live, plan)
    bought(wg, plan)
    assert_same_after_restart(live, plan)
    at_day(wg, 5)
    assert_same_after_restart(live, plan)
    at_day(wg, 7)
    assert_same_after_restart(live, plan)
    plan.pay_insurance()
    assert_same_after_restart(live, plan)
    assert plan.view().status is PlanStatus.COMPLETED
    assert wg.db.authority_counts() == {"approvals": 1, "challenges": 4, "grants": 4}


@pytest.mark.case("R9")
def test_r9_ready_with_an_expired_grant_needs_a_new_code(live: Live) -> None:
    wg = live.client
    plan = settled_at_t3(wg)
    challenge = wg.challenge(plan.purchase_id)
    code = wg.sms_code(challenge.challenge_id)
    wg.arm(Fault.UNEXPECTED)
    wg.refused(wg.verify_response(challenge.challenge_id, code), ErrorCode.UNEXPECTED_FAILURE)
    assert wg.db.action_status(plan.purchase_id) == ActionStatus.READY

    wg.advance(to=challenge.expires_at)
    authority = wg.db.authority_counts()
    live.restart()
    assert wg.db.authority_counts() == authority
    assert plan.node("purchase").status is ActionStatus.AWAITING_STEPUP
    assert wg.db.fund_operations("PURCHASE") == []

    bought(wg, plan)
    assert_row(wg, BOUGHT)
