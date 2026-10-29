"""The coordinator tick: dependencies, not_before, submission and step-up (SPEC §15.2, §15.5)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from wg.contracts.enums import ActionStatus, Fault, LockScope, PlanStatus, Tier
from wg.core.k5_coordinator import CURSOR
from wg.k6 import events, locks
from wg.k6.gateway import CRASH_EXIT_STATUS

from .conftest import DAD, EPOCH, Desk, Exited, Node

pytestmark = pytest.mark.layer("D")


def snapshot(desk: Desk) -> dict[str, list[dict]]:
    """Everything a tick may change, to compare one tick with two."""
    return {
        table: desk.rows(f"SELECT * FROM {table} ORDER BY rowid")
        for table in (
            "actions",
            "plans",
            "operations",
            "operation_events",
            "reservations",
            "receipts",
            "ledger_entries",
            "challenges",
            "approvals",
            "audit_log",
            "domain_events",
        )
    }


def test_a_yellow_node_is_submitted_once_its_time_comes(desk: Desk):
    plan = desk.plan([Node("rent", 30_000, not_before=EPOCH + timedelta(hours=2))])

    desk.coordinator.tick()
    assert desk.status(plan["rent"]) is ActionStatus.DRAFT
    assert desk.plan_status(plan.plan_id) is PlanStatus.ACTIVE

    desk.coordinator.advance_clock(EPOCH + timedelta(hours=2))
    assert desk.status(plan["rent"]) is ActionStatus.SETTLED
    assert desk.plan_status(plan.plan_id) is PlanStatus.COMPLETED


def test_a_red_node_waits_for_a_code_and_none_is_issued(desk: Desk):
    plan = desk.plan(
        [
            Node("first", 30_000),
            Node("second", 150_000, tier=Tier.RED, depends_on=["first"]),
        ]
    )
    authority = desk.authority()

    desk.coordinator.tick()

    assert desk.status(plan["first"]) is ActionStatus.SETTLED
    # The dependency settled during this tick; the next tick moves the dependant.
    desk.coordinator.tick()
    assert desk.status(plan["second"]) is ActionStatus.AWAITING_STEPUP
    assert desk.authority() == authority
    lines = desk.rows("SELECT * FROM audit_log WHERE event = 'awaiting_code'")
    assert [(line["actor"], line["ref"]) for line in lines] == [("coordinator", plan["second"])]


def test_a_node_waits_for_every_dependency(desk: Desk):
    plan = desk.plan(
        [
            Node("other_bank", 30_000, payee_id=DAD),
            Node("after", 20_000, depends_on=["other_bank"]),
        ]
    )
    desk.coordinator.tick()
    assert desk.status(plan["other_bank"]) is ActionStatus.SUBMITTED
    assert desk.status(plan["after"]) is ActionStatus.DRAFT

    desk.coordinator.advance_clock(EPOCH + timedelta(days=1))
    assert desk.status(plan["other_bank"]) is ActionStatus.SETTLED
    assert desk.status(plan["after"]) is ActionStatus.SETTLED


def test_a_failed_dependency_pauses_its_dependants(desk: Desk):
    plan = desk.plan(
        [
            Node("first", 30_000, not_before=EPOCH + timedelta(days=1)),
            Node("second", 20_000, depends_on=["first"]),
        ]
    )
    desk.tamper(
        "UPDATE actions SET status = 'CANCELLED' WHERE logical_action_id = ?", plan["first"]
    )
    desk.coordinator.tick()

    second = desk.current(plan["second"])
    assert second.status is ActionStatus.PAUSED
    assert second.pause_reasons == ("DEPENDENCY_FAILED",)
    assert desk.plan_status(plan.plan_id) is PlanStatus.PAUSED


def test_two_ticks_equal_one(desk: Desk):
    desk.plan(
        [
            Node("now", 30_000),
            Node("other_bank", 20_000, payee_id=DAD),
            Node("later", 10_000, depends_on=["now"], not_before=EPOCH + timedelta(hours=3)),
            Node("red", 150_000, tier=Tier.RED, depends_on=["now"]),
        ]
    )
    desk.coordinator.tick()
    desk.coordinator.tick()
    once = snapshot(desk)

    desk.coordinator.tick()
    assert snapshot(desk) == once


def test_concurrent_ticks_submit_once(desk: Desk):
    plan = desk.plan([Node(f"n{i}", 10_000) for i in range(4)])
    with ThreadPoolExecutor(4) as pool:
        for future in [pool.submit(desk.coordinator.tick) for _ in range(4)]:
            future.result()
    for logical_action_id in plan.ids.values():
        assert (
            len(
                desk.rows("SELECT * FROM operations WHERE logical_action_id = ?", logical_action_id)
            )
            == 1
        )
    assert desk.rows("SELECT * FROM failure_records") == []


def test_a_ready_node_with_a_valid_grant_is_submitted(desk: Desk):
    action = desk.ready(150_000)

    desk.coordinator.tick()

    assert desk.status(action.logical_action_id) is ActionStatus.SETTLED
    assert desk.rows("SELECT consumed_at FROM challenges")[0]["consumed_at"] is not None


def test_a_ready_node_whose_grant_expired_waits_for_a_new_code(desk: Desk):
    action = desk.ready(150_000)
    desk.advance(timedelta(minutes=5))
    authority = desk.authority()

    desk.coordinator.tick()

    assert desk.status(action.logical_action_id) is ActionStatus.AWAITING_STEPUP
    assert desk.rows("SELECT * FROM operations WHERE kind = 'TRANSFER'") == []
    assert desk.authority() == authority
    challenge = desk.rows("SELECT * FROM challenges")[0]
    assert challenge["invalidation_reason"] == "expired" and challenge["consumed_at"] is None


def test_a_locked_plan_is_only_reconciled(desk: Desk):
    yellow = desk.ready(30_000)
    desk.arm(Fault.BANK_ACK_LOST)
    unknown = desk.ready(20_000)
    desk.submit(unknown)
    desk.db.run_in_uow(
        lambda uow: locks.set_lock(uow, LockScope.CUSTOMER, "cus_main", "test"), "test lock"
    )

    desk.coordinator.tick()

    assert desk.status(yellow.logical_action_id) is ActionStatus.READY
    assert desk.status(unknown.logical_action_id) is ActionStatus.SETTLED
    events_of = desk.rows(
        "SELECT event FROM operation_events e JOIN operations o USING (operation_id)"
        " WHERE o.logical_action_id = ?",
        unknown.logical_action_id,
    )
    assert "RECONCILED" in [row["event"] for row in events_of]


def test_plan_expiry_cancels_unsubmitted_nodes_and_keeps_submitted_ones(desk: Desk):
    expiry = EPOCH + timedelta(hours=12)
    plan = desk.plan(
        [
            Node("other_bank", 30_000, payee_id=DAD),
            Node("red", 150_000, tier=Tier.RED, not_before=EPOCH + timedelta(days=2)),
        ],
        expires_at=expiry,
    )
    desk.coordinator.tick()
    assert desk.status(plan["other_bank"]) is ActionStatus.SUBMITTED

    desk.coordinator.advance_clock(EPOCH + timedelta(days=1))

    assert desk.status(plan["red"]) is ActionStatus.CANCELLED
    assert desk.plan_status(plan.plan_id) is PlanStatus.PARTIAL
    cancelled = desk.rows("SELECT * FROM audit_log WHERE event = 'node_cancelled'")
    assert [(r["ref"], r["decision"], r["at"]) for r in cancelled] == [
        (plan["red"], "PLAN_APPROVAL_EXPIRED", "2026-11-02T21:00:00.000000+08:00")
    ]
    # The submitted payment still settles at 09:00 after the plan ended.
    assert desk.status(plan["other_bank"]) is ActionStatus.SETTLED
    assert desk.rows("SELECT * FROM reservations WHERE state = 'ACTIVE'") == []


def test_tick_crash_ends_the_process_after_the_first_plan_decides(desk: Desk):
    first = desk.plan([Node("a", 10_000)])
    second = desk.plan([Node("b", 10_000)])
    desk.arm(Fault.TICK_CRASH)

    with pytest.raises(Exited) as exited:
        desk.coordinator.tick()

    assert exited.value.status == CRASH_EXIT_STATUS
    assert Fault.TICK_CRASH not in desk.armed()
    # One plan decided before the exit, the other was not reached.
    statuses = {desk.status(first["a"]), desk.status(second["b"])}
    assert statuses == {ActionStatus.READY, ActionStatus.DRAFT}
    # After the restart the next tick finishes the work once.
    desk.coordinator.tick()
    for composed, name in ((first, "a"), (second, "b")):
        assert desk.status(composed[name]) is ActionStatus.SETTLED


def test_events_are_consumed_through_the_cursor(desk: Desk):
    plan = desk.plan([Node("other_bank", 30_000, payee_id=DAD)])
    desk.coordinator.tick()
    assert desk.coordinator.process_events() > 0
    assert desk.coordinator.process_events() == 0
    position = desk.rows("SELECT last_event_id FROM event_cursors WHERE consumer = ?", CURSOR)
    with desk.db.read() as conn:
        assert (
            position[0]["last_event_id"] == events.unprocessed_events(conn, "nobody")[-1].event_id
        )

    # Replaying every event from the start changes nothing.
    before = snapshot(desk)
    desk.tamper("DELETE FROM event_cursors")
    assert desk.coordinator.process_events() > 0
    assert snapshot(desk) == before
    assert desk.status(plan["other_bank"]) is ActionStatus.SUBMITTED
