"""K8: the breaker, integrity locks, writes under a lock and operator unlock (SPEC §16, T13)."""

import pytest

from wg.contracts.enums import ActionStatus, Fault, LockScope, OperationStatus
from wg.contracts.errors import ErrorCode, WgError
from wg.core import k8_audit as k8
from wg.k6 import bank, failures, locks
from wg.k6.simtime import now
from wg.services.actions import ActionService

from ..core.drafts import STEP_UP
from .conftest import DAD, Desk

pytestmark = pytest.mark.layer("D")


def active_locks(desk: Desk) -> list[dict]:
    return desk.rows("SELECT * FROM locks WHERE cleared_at IS NULL")


def breaker(desk: Desk, plan_id: str | None) -> int:
    assert plan_id is not None
    with desk.db.read() as conn:
        return failures.breaker_count(conn, plan_id)


def refused(desk: Desk, logical_action_id: str, code: ErrorCode) -> None:
    with pytest.raises(WgError) as error:
        desk.gateway.submit("cus_main", logical_action_id)
    assert error.value.code is code


def unlock(desk: Desk, lock_id: str, reason: str):
    return desk.db.run_in_uow(
        lambda uow: k8.unlock(
            uow, STEP_UP, lock_id=lock_id, operator_id="opr_ops", reason=reason, now=now(uow)
        ),
        "test unlock",
    )


def lock_customer(desk: Desk) -> str:
    return desk.db.run_in_uow(
        lambda uow: locks.set_lock(uow, LockScope.CUSTOMER, "cus_main", "test").lock_id,
        "test lock",
    )


# Breaker


@pytest.mark.case("T13")
def test_three_unexpected_failures_lock_the_plan(desk: Desk):
    action = desk.confirmed(50_000)
    desk.arm(Fault.UNEXPECTED, count=3)
    for _ in range(3):
        refused(desk, action.logical_action_id, ErrorCode.UNEXPECTED_FAILURE)

    (lock,) = active_locks(desk)
    assert (lock["scope"], lock["scope_ref"]) == ("plan", action.plan_id)
    assert lock["reason"] == "breaker: 3 unexpected failures"
    assert breaker(desk, action.plan_id) == 3
    refused(desk, action.logical_action_id, ErrorCode.LOCKED_WRITE)
    assert desk.rows("SELECT * FROM operations WHERE kind = 'TRANSFER'") == []


@pytest.mark.case("T13")
def test_a_success_resets_the_count(desk: Desk):
    action = desk.confirmed(50_000)
    desk.arm(Fault.UNEXPECTED, count=2)
    for _ in range(2):
        refused(desk, action.logical_action_id, ErrorCode.UNEXPECTED_FAILURE)
    assert breaker(desk, action.plan_id) == 2

    desk.submit(action)

    assert breaker(desk, action.plan_id) == 0
    assert active_locks(desk) == []


def test_expected_refusals_are_not_counted(desk: Desk):
    waiting = desk.confirmed(150_000)  # AWAITING_STEPUP: the gateway refuses it
    for _ in range(4):
        refused(desk, waiting.logical_action_id, ErrorCode.APPROVAL_REQUIRED)
    assert desk.rows("SELECT * FROM failure_records") == []
    assert breaker(desk, waiting.plan_id) == 0
    assert active_locks(desk) == []


def test_busy_retry_is_not_counted(desk: Desk):
    action = desk.confirmed(50_000)
    for _ in range(3):
        desk.arm(Fault.BUSY, count=4)
        refused(desk, action.logical_action_id, ErrorCode.BUSY_RETRY)
    assert desk.rows("SELECT * FROM failure_records") == []
    assert breaker(desk, action.plan_id) == 0
    assert active_locks(desk) == []


@pytest.mark.case("T13")
def test_an_unknown_outcome_neither_counts_nor_resets(desk: Desk):
    action = desk.confirmed(50_000)
    desk.arm(Fault.UNEXPECTED, count=2)
    for _ in range(2):
        refused(desk, action.logical_action_id, ErrorCode.UNEXPECTED_FAILURE)
    desk.arm(Fault.BANK_ACK_LOST)

    assert desk.submit(action).operation.status is OperationStatus.UNKNOWN

    assert breaker(desk, action.plan_id) == 2
    assert active_locks(desk) == []


# Integrity


@pytest.mark.case("R4")
def test_a_missing_bank_record_locks_the_customer(desk: Desk):
    desk.arm(Fault.BANK_ACK_LOST)
    unknown = desk.submit(desk.ready(30_000)).operation
    desk.tamper("DELETE FROM bank_requests WHERE idempotency_key = ?", unknown.idempotency_key)

    with pytest.raises(WgError) as error:
        desk.gateway.reconcile(unknown.operation_id)

    assert error.value.code is ErrorCode.INTEGRITY_VIOLATION
    (lock,) = active_locks(desk)
    assert (lock["scope"], lock["scope_ref"], lock["reason"]) == (
        "customer",
        "cus_main",
        "INTEGRITY_VIOLATION: no_bank_record",
    )
    (record,) = desk.rows("SELECT * FROM failure_records")
    assert record["error_class"] == "integrity" and "no_bank_record" in record["detail"]


def test_an_unbalanced_ledger_posting_locks_the_customer(desk: Desk):
    settled = desk.submit(desk.ready(10_000)).operation

    with pytest.raises(WgError):
        desk.db.run_in_uow(
            lambda uow: bank.post_entries(
                uow, settled.operation_id, (bank.Posting("acc_main", -1),), now(uow)
            ),
            "test posting",
        )

    (lock,) = active_locks(desk)
    assert (lock["scope"], lock["reason"]) == ("customer", "INTEGRITY_VIOLATION: ledger_sum")


def test_a_grant_whose_scope_changed_locks_the_plan(desk: Desk):
    action = desk.ready(150_000)
    desk.tamper(
        "UPDATE actions SET params_json = replace(params_json, 'pay_mom', 'pay_wangfang')"
        " WHERE logical_action_id = ?",
        action.logical_action_id,
    )

    refused(desk, action.logical_action_id, ErrorCode.INTEGRITY_VIOLATION)

    (lock,) = active_locks(desk)
    assert (lock["scope"], lock["scope_ref"]) == ("plan", action.plan_id)
    assert lock["reason"] == "INTEGRITY_VIOLATION: scope_hash"


# Under a lock


@pytest.mark.case("T13")
def test_a_lock_blocks_a_transfer_but_not_reconciliation(desk: Desk):
    ready = desk.confirmed(10_000)
    desk.arm(Fault.BANK_ACK_LOST)
    unknown = desk.submit(desk.ready(30_000)).operation
    lock_customer(desk)

    refused(desk, ready.logical_action_id, ErrorCode.LOCKED_WRITE)
    reconciled = desk.gateway.reconcile(unknown.operation_id)

    assert reconciled.status is OperationStatus.SETTLED
    assert desk.status(ready.logical_action_id) is ActionStatus.READY
    assert (
        len(desk.rows("SELECT * FROM receipts WHERE operation_id = ?", unknown.operation_id)) == 1
    )


def test_a_lock_allows_cancelling_an_unsubmitted_action(desk: Desk):
    ready = desk.confirmed(10_000)
    lock_customer(desk)

    result = ActionService(desk.db, STEP_UP, desk.gateway).cancel(
        "cus_main", ready.logical_action_id
    )

    assert result.status is ActionStatus.CANCELLED
    assert desk.plan_status(ready.plan_id).value == "partial"


def test_settlements_continue_under_a_lock(desk: Desk):
    submitted = desk.submit(desk.ready(30_000, DAD)).operation
    lock_customer(desk)

    desk.advance_to(submitted.settle_due_at)
    desk.gateway.settle_due()

    rows = desk.rows("SELECT status FROM operations WHERE operation_id = ?", submitted.operation_id)
    assert rows[0]["status"] == OperationStatus.SETTLED.value


# Unlock


@pytest.mark.case("T13")
def test_unlock_requires_a_reason(desk: Desk):
    lock_id = lock_customer(desk)
    for blank in ("", "   "):
        with pytest.raises(WgError) as error:
            unlock(desk, lock_id, blank)
        assert error.value.code is ErrorCode.UNSUPPORTED_REQUEST
    assert len(active_locks(desk)) == 1

    cleared = unlock(desk, lock_id, "已核对全部操作")

    assert cleared.clear_reason == "已核对全部操作" and cleared.cleared_by == "opr_ops"
    (line,) = desk.rows("SELECT * FROM audit_log WHERE event = 'lock_cleared'")
    assert (line["actor"], line["reason"]) == ("operator", "已核对全部操作")


@pytest.mark.case("T13")
def test_unlock_invalidates_grants_and_resumes_nothing(desk: Desk):
    red = desk.ready(150_000)
    yellow = desk.confirmed(10_000)
    lock_id = lock_customer(desk)
    authority = desk.authority()

    unlock(desk, lock_id, "已核对全部操作")

    assert desk.status(red.logical_action_id) is ActionStatus.AWAITING_STEPUP
    assert desk.status(yellow.logical_action_id) is ActionStatus.READY
    (grant,) = desk.rows(
        "SELECT * FROM challenges WHERE logical_action_id = ?", red.logical_action_id
    )
    assert grant["invalidation_reason"] == "lock_cleared" and grant["consumed_at"] is None
    assert desk.rows("SELECT * FROM operations WHERE kind = 'TRANSFER'") == []
    assert desk.authority() == authority


@pytest.mark.case("T13")
def test_unlock_reconciles_its_scope_first(desk: Desk):
    desk.arm(Fault.BANK_ACK_LOST)
    unknown = desk.submit(desk.ready(30_000)).operation
    lock_id = lock_customer(desk)

    unlock(desk, lock_id, "已核对全部操作")

    rows = desk.rows("SELECT status FROM operations WHERE operation_id = ?", unknown.operation_id)
    assert rows[0]["status"] == OperationStatus.SETTLED.value


def test_unlock_is_refused_while_an_operation_cannot_be_reconciled(desk: Desk):
    desk.arm(Fault.BANK_ACK_LOST)
    unknown = desk.submit(desk.ready(30_000)).operation
    desk.tamper("DELETE FROM bank_requests WHERE idempotency_key = ?", unknown.idempotency_key)
    lock_id = lock_customer(desk)

    with pytest.raises(WgError) as error:
        unlock(desk, lock_id, "已核对全部操作")

    assert error.value.code is ErrorCode.INTEGRITY_VIOLATION
    assert [row["lock_id"] for row in active_locks(desk)] == [lock_id]


def test_an_unknown_or_cleared_lock_is_refused(desk: Desk):
    with pytest.raises(WgError) as error:
        unlock(desk, "lck_0000000000000000", "原因")
    assert error.value.code is ErrorCode.NOT_FOUND
    lock_id = lock_customer(desk)
    unlock(desk, lock_id, "原因")
    with pytest.raises(WgError) as error:
        unlock(desk, lock_id, "原因")
    assert error.value.code is ErrorCode.UNSUPPORTED_REQUEST
