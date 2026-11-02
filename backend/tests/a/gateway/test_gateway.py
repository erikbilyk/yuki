import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from wg.contracts.enums import (
    ActionStatus,
    Fault,
    OperationEventKind,
    OperationStatus,
    ReceiptStatus,
    ReservationState,
    Tier,
)
from wg.contracts.errors import ErrorCode, WgError
from wg.core import k3_policy
from wg.core import k4_approval as k4
from wg.k6 import bank, operations
from wg.k6.gateway import settle_due
from wg.k6.operations import StaleOperation
from wg.k6.simtime import SHANGHAI, now
from wg.packs.registry import pack_for
from wg.packs.transfer import next_settlement

from ..core.drafts import MAIN, STEP_UP
from .conftest import DAD, START_FEN, Bench

pytestmark = pytest.mark.layer("D")

EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)


def operation_rows(bench: Bench, logical_action_id: str) -> list[dict]:
    return bench.rows("SELECT * FROM operations WHERE logical_action_id = ?", logical_action_id)


def ledger(bench: Bench, operation_id: str) -> list[dict]:
    return bench.rows("SELECT * FROM ledger_entries WHERE operation_id = ?", operation_id)


def domain_event_types(bench: Bench) -> list[str]:
    return [row["type"] for row in bench.rows("SELECT type FROM domain_events ORDER BY seq")]


# Same-bank transfer


@pytest.mark.case("T1")
def test_same_bank_transfer_settles_in_the_submitting_unit(bench: Bench):
    action = bench.ready(50_000)
    events_before = domain_event_types(bench)

    submission = bench.submit(action)

    operation = submission.operation
    assert submission.created and operation.status is OperationStatus.SETTLED
    assert operation.idempotency_key == f"{action.logical_action_id}:{action.revision}"
    assert operation.amount_fen == 50_000 and operation.payee_id == "pay_mom"
    assert operation.settled_at == operation.submitted_at == EPOCH
    entries = ledger(bench, operation.operation_id)
    assert sum(e["amount_fen"] for e in entries) == 0
    assert [e["amount_fen"] for e in entries if e["account_id"] == "acc_main"] == [-50_000]
    assert bench.balance() == START_FEN - 50_000
    with bench.db.read() as conn:
        assert operations.operation_events(conn, operation.operation_id) == [
            OperationEventKind.SUBMITTED,
            OperationEventKind.SETTLED,
        ]
        receipt = operations.receipt_of(conn, operation.operation_id)
        reservation = operations.reservation_of(conn, operation.operation_id)
        approval = k4.approval_for(conn, action)
    assert receipt is not None and receipt.status is ReceiptStatus.SETTLED
    assert approval is not None and receipt.terms_hash == approval.terms_hash
    assert reservation is not None and reservation.state is ReservationState.CONSUMED
    assert bench.current(action.logical_action_id).status is ActionStatus.SETTLED
    assert domain_event_types(bench)[len(events_before) :] == [
        "operation_submitted",
        "operation_settled",
    ]


@pytest.mark.case("T53")
def test_a_repeat_with_the_same_key_returns_the_original(bench: Bench):
    action = bench.ready(50_000)
    first = bench.submit(action)
    again = bench.submit(action)

    assert not again.created and again.operation.operation_id == first.operation.operation_id
    assert len(operation_rows(bench, action.logical_action_id)) == 1
    assert len(ledger(bench, first.operation.operation_id)) == 2
    assert len(bench.rows("SELECT * FROM receipts")) == 1
    assert len(bench.rows("SELECT * FROM reservations")) == 1
    assert bench.balance() == START_FEN - 50_000


@pytest.mark.case("T53")
def test_concurrent_submits_of_one_action_give_one_effect(bench: Bench):
    action = bench.ready(50_000)
    start = threading.Barrier(4)

    def submit(_: int):
        start.wait()
        return bench.submit(action)

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(submit, range(4)))

    assert len({r.operation.operation_id for r in results}) == 1
    assert sum(r.created for r in results) == 1
    assert len(bench.rows("SELECT * FROM reservations")) == 1
    assert len(bench.rows("SELECT * FROM receipts")) == 1
    assert bench.balance() == START_FEN - 50_000


def test_the_same_key_with_another_request_hash_is_refused(bench: Bench):
    action = bench.ready(50_000)

    def conflicting(uow):
        operations.insert_operation(
            uow,
            key=operations.idempotency_key(action.logical_action_id, action.revision),
            request_hash="0" * 64,
            kind="TRANSFER",
            submitted_at=now(uow),
            logical_action_id=action.logical_action_id,
            revision=action.revision,
            customer_id=MAIN,
            account_id="acc_main",
            amount_fen=50_000,
        )

    bench.db.run_in_uow(conflicting, "test conflicting operation")
    with pytest.raises(WgError) as refused:
        bench.submit(action)
    assert refused.value.code is ErrorCode.IDEMPOTENCY_MISMATCH
    [stored] = operation_rows(bench, action.logical_action_id)
    assert ledger(bench, stored["operation_id"]) == []
    assert bench.balance() == START_FEN


def test_ledger_entries_sum_to_zero_for_every_operation(bench: Bench):
    bench.submit(bench.ready(50_000))
    bench.submit(bench.ready(30_000, DAD))
    bench.advance(timedelta(days=1))
    bench.gateway.settle_due()
    sums = bench.rows(
        "SELECT o.operation_id, COALESCE(SUM(l.amount_fen), 0) AS total, COUNT(l.entry_id) AS n"
        " FROM operations o LEFT JOIN ledger_entries l ON l.operation_id = o.operation_id"
        " GROUP BY o.operation_id"
    )
    assert len(sums) == 3  # opening balances and the two transfers
    assert all(row["total"] == 0 and row["n"] >= 2 for row in sums)
    customer_total = bench.rows(
        "SELECT SUM(balance_fen) AS total FROM accounts WHERE kind = 'customer'"
    )[0]["total"]
    internal_total = bench.rows(
        "SELECT SUM(balance_fen) AS total FROM accounts WHERE kind = 'internal'"
    )[0]["total"]
    assert customer_total + internal_total == 0


def test_a_reservation_is_created_once_and_closed_once(bench: Bench):
    operation = bench.submit(bench.ready(50_000)).operation
    with bench.db.read() as conn:
        reservation = operations.reservation_of(conn, operation.operation_id)
    assert reservation is not None and reservation.state is ReservationState.CONSUMED

    with pytest.raises(StaleOperation):
        bench.db.run_in_uow(
            lambda uow: operations.close_reservation(uow, reservation, ReservationState.RELEASED),
            "test close again",
        )
    with pytest.raises(sqlite3.IntegrityError):
        bench.db.run_in_uow(
            lambda uow: uow.execute(
                "UPDATE reservations SET state = 'ACTIVE' WHERE operation_id = ?",
                (operation.operation_id,),
            ),
            "test reactivate",
        )
    with pytest.raises(sqlite3.IntegrityError):
        bench.db.run_in_uow(
            lambda uow: operations.insert_reservation(
                uow, operation_id=operation.operation_id, account_id="acc_main", amount_fen=1
            ),
            "test second reservation",
        )


def test_a_bank_rejection_releases_the_reservation(bench: Bench):
    action = bench.ready(50_000)
    bench.arm(Fault.SETTLEMENT_REJECT)

    operation = bench.submit(action).operation

    assert operation.status is OperationStatus.REJECTED
    assert ledger(bench, operation.operation_id) == []
    assert bench.balance() == START_FEN
    with bench.db.read() as conn:
        receipt = operations.receipt_of(conn, operation.operation_id)
        reservation = operations.reservation_of(conn, operation.operation_id)
        day_sum = k3_policy.daily_sum(conn, MAIN, EPOCH.date())
    assert receipt is not None and receipt.status is ReceiptStatus.REJECTED
    assert receipt.effect["reason"] == bank.SETTLEMENT_REJECTED
    assert reservation is not None and reservation.state is ReservationState.RELEASED
    assert bench.current(action.logical_action_id).status is ActionStatus.REJECTED
    assert day_sum == 0


# Day total and step-up


@pytest.mark.case("T6")
def test_a_yellow_action_whose_day_total_became_red_gets_tier_changed(bench: Bench):
    first = bench.ready(80_000)
    second = bench.ready(70_000)
    assert first.tier is Tier.YELLOW and second.tier is Tier.YELLOW
    bench.submit(first)

    with pytest.raises(WgError) as refused:
        bench.submit(second)

    assert refused.value.code is ErrorCode.TIER_CHANGED
    assert operation_rows(bench, second.logical_action_id) == []
    waiting = bench.current(second.logical_action_id)
    assert waiting.status is ActionStatus.AWAITING_STEPUP

    bench.db.run_in_uow(
        lambda uow: k4.request_challenge(
            uow,
            STEP_UP,
            customer_id=MAIN,
            logical_action_id=second.logical_action_id,
            now=now(uow),
        ),
        "test challenge",
    )
    ready = bench.verify(waiting)
    operation = bench.submit(ready).operation
    assert operation.status is OperationStatus.SETTLED
    grants = bench.rows(
        "SELECT consumed_at FROM challenges WHERE logical_action_id = ?", second.logical_action_id
    )
    assert [g["consumed_at"] is not None for g in grants] == [True]
    assert bench.balance() == START_FEN - 150_000


@pytest.mark.case("T6")
def test_parallel_yellow_submits_never_both_execute(bench: Bench):
    actions = [bench.ready(80_000), bench.ready(70_000)]
    start = threading.Barrier(2)

    def submit(action):
        start.wait()
        try:
            return bench.submit(action)
        except WgError as error:
            return error

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(submit, actions))

    refused = [r for r in results if isinstance(r, WgError)]
    assert [r.code for r in refused] == [ErrorCode.TIER_CHANGED]
    executed = len(bench.rows("SELECT * FROM operations WHERE kind = 'TRANSFER'"))
    assert executed == 1


@pytest.mark.case("T6")
def test_a_red_action_needs_its_grant_at_submission(bench: Bench):
    action = bench.confirmed(150_000)
    assert action.status is ActionStatus.AWAITING_STEPUP
    with pytest.raises(WgError) as refused:
        bench.submit(action)
    assert refused.value.code is ErrorCode.APPROVAL_REQUIRED

    operation = bench.submit(bench.verify(action)).operation
    assert operation.status is OperationStatus.SETTLED


def test_the_daily_limit_is_checked_again_at_submission(bench: Bench):
    action = bench.ready(50_000)

    def spend_the_day(uow):
        operations.insert_operation(
            uow,
            key="test:limit",
            request_hash="1" * 64,
            kind="TRANSFER",
            submitted_at=now(uow),
            status=OperationStatus.SETTLED,
            customer_id=MAIN,
            account_id="acc_main",
            amount_fen=k3_policy.DAILY_TRANSFER_LIMIT_FEN,
        )

    bench.db.run_in_uow(spend_the_day, "test day total")
    with pytest.raises(WgError) as refused:
        bench.submit(action)
    assert refused.value.code is ErrorCode.DAILY_LIMIT_EXCEEDED
    assert operation_rows(bench, action.logical_action_id) == []


# Headroom and funds


@pytest.mark.case("T57")
def test_headroom_is_recomputed_from_current_rows_at_submission(bench: Bench):
    action = bench.ready(50_000)
    # Protection rises after the draft: the reserve goes from ¥4,000 to ¥5,800, so the
    # headroom of ¥2,000 falls to ¥200.
    bench.db.run_in_uow(
        lambda uow: uow.execute(
            "UPDATE reserves SET target_fen = 580000 WHERE customer_id = ?", (MAIN,)
        ),
        "test reserve",
    )

    with pytest.raises(WgError) as refused:
        bench.submit(action)

    assert refused.value.code is ErrorCode.INSUFFICIENT_HEADROOM
    assert refused.value.detail["headroom_fen"] == 20_000
    assert operation_rows(bench, action.logical_action_id) == []
    assert bench.rows("SELECT * FROM reservations") == []
    assert bench.current(action.logical_action_id).status is ActionStatus.READY


@pytest.mark.case("T57")
def test_an_unsettled_hold_counts_against_headroom(bench: Bench):
    # ¥600 to an other-bank payee is held until tomorrow 09:00: headroom ¥2,000 → ¥1,400.
    held = bench.submit(bench.ready(60_000, DAD)).operation
    assert held.status is OperationStatus.SUBMITTED
    action = bench.ready(30_000)
    larger = bench.draft(150_000)
    with bench.db.read() as conn:
        reasons = pack_for(larger.kind).precheck(conn, MAIN, larger.kind, larger.params, EPOCH)
    assert [r.code for r in reasons] == [ErrorCode.INSUFFICIENT_HEADROOM]
    assert reasons[0].detail["headroom_fen"] == 140_000

    assert bench.submit(action).operation.status is OperationStatus.SETTLED


def test_insufficient_posted_money_is_refused_at_submission(bench: Bench):
    action = bench.ready(50_000)
    # ¥17,900 leaves the account outside YUKI after the transfer was approved.
    _drain(bench, 1_790_000)

    with pytest.raises(WgError) as refused:
        bench.submit(action)
    assert refused.value.code is ErrorCode.INSUFFICIENT_FUNDS
    assert refused.value.detail["shortfall_fen"] == 40_000
    assert operation_rows(bench, action.logical_action_id) == []


def _drain(bench: Bench, amount_fen: int) -> None:
    def drain(uow):
        operation = operations.insert_operation(
            uow,
            key="test:drain",
            request_hash="2" * 64,
            kind="EXTERNAL_DEBIT",
            submitted_at=now(uow),
            status=OperationStatus.SETTLED,
            customer_id=MAIN,
            account_id="acc_main",
            amount_fen=amount_fen,
        )
        bank.post_entries(
            uow,
            operation.operation_id,
            (bank.Posting("acc_main", -amount_fen), bank.Posting("acc_sys_clearing", amount_fen)),
            now(uow),
        )

    bench.db.run_in_uow(drain, "test drain")


# Other-bank settlement


def test_next_settlement_is_the_first_0900_after_submission():
    assert next_settlement(EPOCH) == EPOCH + timedelta(days=1)
    early = datetime(2026, 11, 3, 8, 59, tzinfo=SHANGHAI)
    assert next_settlement(early) == datetime(2026, 11, 3, 9, 0, tzinfo=SHANGHAI)
    late = datetime(2026, 11, 3, 23, 30, tzinfo=SHANGHAI)
    assert next_settlement(late) == datetime(2026, 11, 4, 9, 0, tzinfo=SHANGHAI)


@pytest.mark.case("R3")
def test_an_other_bank_transfer_settles_at_the_next_0900(bench: Bench):
    action = bench.ready(30_000, DAD)
    operation = bench.submit(action).operation

    assert operation.status is OperationStatus.SUBMITTED
    assert operation.settle_due_at == EPOCH + timedelta(days=1)
    assert ledger(bench, operation.operation_id) == []
    assert bench.rows("SELECT * FROM receipts") == []
    assert bench.balance() == START_FEN
    with bench.db.read() as conn:
        assert operations.reserved_fen(conn, "acc_main") == 30_000
        assert k3_policy.daily_sum(conn, MAIN, EPOCH.date()) == 30_000
    assert bench.current(action.logical_action_id).status is ActionStatus.SUBMITTED

    bench.advance_to(EPOCH + timedelta(days=1, minutes=-1))
    assert bench.gateway.settle_due() == []

    bench.advance_to(EPOCH + timedelta(days=1, hours=3))
    settled = bench.gateway.settle_due()
    assert [op.status for op in settled] == [OperationStatus.SETTLED]
    assert settled[0].settled_at == EPOCH + timedelta(days=1)
    assert len(bench.rows("SELECT * FROM receipts")) == 1
    assert bench.balance() == START_FEN - 30_000
    assert bench.current(action.logical_action_id).status is ActionStatus.SETTLED

    # A repeated settlement changes nothing (T56): one receipt, one set of entries.
    assert bench.gateway.settle_due() == []
    again = bench.db.run_in_uow(lambda uow: settle_due(uow, now(uow)), "test settle again")
    assert again == []
    assert len(ledger(bench, operation.operation_id)) == 2
    assert len(bench.rows("SELECT * FROM receipts")) == 1


def test_an_other_bank_transfer_rejected_at_settlement(bench: Bench):
    operation = bench.submit(bench.ready(30_000, DAD)).operation
    bench.arm(Fault.SETTLEMENT_REJECT)
    bench.advance(timedelta(days=1))

    [rejected] = bench.gateway.settle_due()

    assert rejected.status is OperationStatus.REJECTED
    assert ledger(bench, operation.operation_id) == []
    assert bench.balance() == START_FEN
    with bench.db.read() as conn:
        assert operations.reserved_fen(conn, "acc_main") == 0
    receipt = bench.rows("SELECT * FROM receipts")
    assert [r["status"] for r in receipt] == ["REJECTED"]


# UNKNOWN and reconciliation


@pytest.mark.case("R4")
def test_bank_ack_lost_leaves_unknown_and_reconcile_restores_the_receipt(bench: Bench):
    action = bench.ready(50_000)
    bench.arm(Fault.BANK_ACK_LOST)

    unknown = bench.submit(action).operation

    assert unknown.status is OperationStatus.UNKNOWN
    assert bench.current(action.logical_action_id).status is ActionStatus.UNKNOWN
    assert bench.rows("SELECT * FROM receipts") == []
    with bench.db.read() as conn:
        bank_request = bank.find_request(conn, unknown.idempotency_key)
        assert k3_policy.daily_sum(conn, MAIN, EPOCH.date()) == 50_000
    assert bank_request is not None and bank_request.settled
    assert bench.balance() == START_FEN - 50_000  # the bank's effect is there

    reconciled = bench.gateway.reconcile(unknown.operation_id)

    assert reconciled.status is OperationStatus.SETTLED
    assert reconciled.idempotency_key == unknown.idempotency_key
    assert len(operation_rows(bench, action.logical_action_id)) == 1
    assert bench.current(action.logical_action_id).status is ActionStatus.SETTLED
    with bench.db.read() as conn:
        assert operations.operation_events(conn, unknown.operation_id) == [
            OperationEventKind.SUBMITTED,
            OperationEventKind.UNKNOWN_OBSERVED,
            OperationEventKind.RECONCILED,
            OperationEventKind.SETTLED,
        ]
        reservation = operations.reservation_of(conn, unknown.operation_id)
    assert reservation is not None and reservation.state is ReservationState.CONSUMED
    assert len(bench.rows("SELECT * FROM receipts")) == 1
    assert len(ledger(bench, unknown.operation_id)) == 2
    assert bench.balance() == START_FEN - 50_000

    # Reconciling a terminal operation changes nothing; a resubmission returns it.
    assert bench.gateway.reconcile(unknown.operation_id) == reconciled
    assert bench.submit(action).operation == reconciled


@pytest.mark.case("R4")
def test_an_unknown_other_bank_transfer_reconciles_to_submitted_then_settles(bench: Bench):
    action = bench.ready(30_000, DAD)
    bench.arm(Fault.BANK_ACK_LOST)
    unknown = bench.submit(action).operation
    assert unknown.status is OperationStatus.UNKNOWN

    assert bench.gateway.reconcile(unknown.operation_id).status is OperationStatus.SUBMITTED
    assert bench.current(action.logical_action_id).status is ActionStatus.SUBMITTED
    bench.advance(timedelta(days=1))
    [settled] = bench.gateway.settle_due()
    assert settled.status is OperationStatus.SETTLED


@pytest.mark.case("R4")
def test_settlement_reconciles_an_unknown_operation_first(bench: Bench):
    bench.arm(Fault.BANK_ACK_LOST)
    unknown = bench.submit(bench.ready(30_000, DAD)).operation
    bench.advance(timedelta(days=1))

    [settled] = bench.gateway.settle_due()

    assert settled.operation_id == unknown.operation_id
    assert settled.status is OperationStatus.SETTLED


@pytest.mark.case("R4")
def test_an_unknown_operation_without_a_bank_record_is_an_integrity_violation(bench: Bench):
    bench.arm(Fault.BANK_ACK_LOST)
    unknown = bench.submit(bench.ready(50_000)).operation
    bench.db.run_in_uow(
        lambda uow: uow.execute(
            "DELETE FROM bank_requests WHERE idempotency_key = ?", (unknown.idempotency_key,)
        ),
        "test delete bank record",
    )

    with pytest.raises(WgError) as refused:
        bench.gateway.reconcile(unknown.operation_id)

    assert refused.value.code is ErrorCode.INTEGRITY_VIOLATION
    failures = bench.rows("SELECT error_class FROM failure_records")
    assert [f["error_class"] for f in failures] == ["integrity"]
    assert (
        bench.rows("SELECT status FROM operations WHERE operation_id = ?", unknown.operation_id)[0][
            "status"
        ]
        == "UNKNOWN"
    )


# State refusals


def test_only_a_ready_action_is_submitted(bench: Bench):
    draft = bench.draft(50_000)
    with pytest.raises(WgError) as refused:
        bench.gateway.submit(MAIN, draft.logical_action_id)
    assert refused.value.code is ErrorCode.APPROVAL_REQUIRED
    assert bench.rows("SELECT * FROM operations WHERE kind = 'TRANSFER'") == []


def test_a_foreign_action_is_not_found(bench: Bench):
    action = bench.ready(50_000)
    with pytest.raises(WgError) as refused:
        bench.gateway.submit("cus_other", action.logical_action_id)
    assert refused.value.code is ErrorCode.NOT_FOUND
