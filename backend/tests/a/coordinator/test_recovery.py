"""Restart recovery at process start (SPEC §12.8, R9): consistent state, no new authority."""

from datetime import timedelta
from pathlib import Path

import pytest

from wg.contracts.enums import ActionStatus, Fault, OperationStatus, Tier
from wg.core.k5_coordinator import Coordinator
from wg.k6.db import Database
from wg.k6.gateway import Gateway
from wg.k6.migrate import migrate

from ..core.drafts import STEP_UP
from .conftest import DAD, EPOCH, Desk, Exited, Node

pytestmark = pytest.mark.layer("D")


def operations_of(desk: Desk, logical_action_id: str) -> list[dict]:
    return desk.rows("SELECT * FROM operations WHERE logical_action_id = ?", logical_action_id)


@pytest.mark.case("R9")
def test_ready_with_an_expired_grant_needs_a_new_code_after_restart(desk: Desk):
    action = desk.ready(150_000)
    desk.advance(timedelta(minutes=5))
    authority = desk.authority()

    desk.coordinator.recover()

    assert desk.status(action.logical_action_id) is ActionStatus.AWAITING_STEPUP
    assert operations_of(desk, action.logical_action_id) == []
    assert desk.authority() == authority


@pytest.mark.case("R9")
def test_restart_creates_no_approval_challenge_or_grant(desk: Desk):
    desk.confirmed(150_000)  # AWAITING_STEPUP with an open challenge
    desk.ready(120_000)  # READY with a grant
    desk.confirmed(10_000)  # READY, YELLOW, never submitted
    desk.submit(desk.ready(20_000, DAD))  # SUBMITTED to another bank
    desk.plan([Node("red", 150_000, tier=Tier.RED)])  # a plan node due now
    authority = desk.authority()

    desk.coordinator.recover()
    desk.coordinator.recover()

    assert desk.authority() == authority
    assert desk.rows("SELECT * FROM sms_outbox WHERE code IS NOT NULL") != []


@pytest.mark.case("T11")
def test_a_ready_action_whose_submission_crashed_is_submitted_once_with_its_key(desk: Desk):
    action = desk.confirmed(50_000)
    desk.arm(Fault.CRASH_BEFORE_COMMIT)
    with pytest.raises(Exited):
        desk.submit(action)
    assert operations_of(desk, action.logical_action_id) == []

    desk.coordinator.recover()

    (operation,) = operations_of(desk, action.logical_action_id)
    assert operation["idempotency_key"] == f"{action.logical_action_id}:1"
    assert operation["status"] == OperationStatus.SETTLED.value
    # The client repeats its request after the restart: the original operation comes back.
    repeat = desk.submit(action)
    assert not repeat.created and repeat.operation.operation_id == operation["operation_id"]


@pytest.mark.case("R4")
def test_restart_reconciles_an_unknown_outcome_by_the_same_key(desk: Desk):
    action = desk.ready(30_000)
    desk.arm(Fault.BANK_ACK_LOST)
    unknown = desk.submit(action).operation
    assert unknown.status is OperationStatus.UNKNOWN

    desk.coordinator.recover()

    (operation,) = operations_of(desk, action.logical_action_id)
    assert operation["operation_id"] == unknown.operation_id
    assert operation["status"] == OperationStatus.SETTLED.value
    events = desk.rows(
        "SELECT event FROM operation_events WHERE operation_id = ? ORDER BY rowid",
        unknown.operation_id,
    )
    assert [e["event"] for e in events] == [
        "SUBMITTED",
        "UNKNOWN_OBSERVED",
        "RECONCILED",
        "SETTLED",
    ]
    assert (
        len(desk.rows("SELECT * FROM receipts WHERE operation_id = ?", unknown.operation_id)) == 1
    )


@pytest.mark.case("R3")
def test_restart_applies_due_settlements_without_moving_the_clock(desk: Desk):
    operation = desk.submit(desk.ready(30_000, DAD)).operation
    target = EPOCH + timedelta(days=1)
    desk.advance_to(target)  # the clock moved, the process died before settling

    desk.coordinator.recover()

    settled = desk.rows("SELECT * FROM operations WHERE operation_id = ?", operation.operation_id)
    assert settled[0]["status"] == OperationStatus.SETTLED.value
    assert desk.rows("SELECT now FROM sim_clock")[0]["now"] == "2026-11-03T09:00:00.000000+08:00"
    assert (
        len(desk.rows("SELECT * FROM receipts WHERE operation_id = ?", operation.operation_id)) == 1
    )


@pytest.mark.case("R4")
def test_restart_without_a_bank_record_locks_and_finishes(desk: Desk):
    desk.arm(Fault.BANK_ACK_LOST)
    unknown = desk.submit(desk.ready(30_000)).operation
    desk.tamper("DELETE FROM bank_requests WHERE idempotency_key = ?", unknown.idempotency_key)
    other = desk.confirmed(10_000)

    desk.coordinator.recover()

    (lock,) = desk.rows("SELECT * FROM locks WHERE cleared_at IS NULL")
    assert (lock["scope"], lock["scope_ref"]) == ("customer", "cus_main")
    assert lock["reason"] == "INTEGRITY_VIOLATION: no_bank_record"
    (row,) = desk.rows("SELECT status FROM operations WHERE operation_id = ?", unknown.operation_id)
    assert row["status"] == OperationStatus.UNKNOWN.value
    # Under the customer lock the READY action is held, not submitted.
    assert desk.status(other.logical_action_id) is ActionStatus.READY


def test_recovery_on_a_database_without_a_profile_does_nothing(tmp_path: Path):
    db = Database(tmp_path / "empty.db")
    migrate(db)
    Coordinator(db, Gateway(db, fault_wait_file=tmp_path / "fault.wait"), STEP_UP).recover()
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 0
    db.close()
