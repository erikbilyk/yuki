"""The chronological clock walk behind /sim/clock/advance (SPEC §15.3, §15.8, R2)."""

from datetime import timedelta

import pytest

from wg.contracts.enums import ActionStatus
from wg.contracts.errors import ErrorCode, WgError

from .conftest import DAD, EPOCH, Desk, Node

pytestmark = pytest.mark.layer("D")


def operation_of(desk: Desk, logical_action_id: str) -> dict:
    (row,) = desk.rows("SELECT * FROM operations WHERE logical_action_id = ?", logical_action_id)
    return row


def stamp(at) -> str:
    return at.isoformat(timespec="microseconds")


def test_one_jump_visits_every_due_instant_in_order(desk: Desk):
    noon = EPOCH + timedelta(days=1, hours=3)
    plan = desk.plan(
        [
            Node("other_bank", 30_000, payee_id=DAD),
            Node("after_settlement", 20_000, depends_on=["other_bank"]),
            Node("at_noon", 10_000, not_before=noon),
        ]
    )
    desk.coordinator.tick()

    desk.coordinator.advance_clock(EPOCH + timedelta(days=3))

    settle_at = EPOCH + timedelta(days=1)
    first = operation_of(desk, plan["other_bank"])
    assert first["settled_at"] == stamp(settle_at)
    # At 09:00 the settlement comes first, then the tick that submits its dependant.
    assert operation_of(desk, plan["after_settlement"])["submitted_at"] == stamp(settle_at)
    assert operation_of(desk, plan["at_noon"])["submitted_at"] == stamp(noon)
    assert all(desk.status(i) is ActionStatus.SETTLED for i in plan.ids.values())
    assert desk.rows("SELECT now FROM sim_clock")[0]["now"] == stamp(EPOCH + timedelta(days=3))


def test_codes_issued_before_a_jump_are_expired_after_it(desk: Desk):
    action = desk.confirmed(150_000)

    desk.coordinator.advance_clock(EPOCH + timedelta(hours=1))

    (challenge,) = desk.rows("SELECT * FROM challenges")
    assert challenge["invalidation_reason"] == "expired"
    assert desk.rows("SELECT * FROM sms_outbox WHERE code IS NOT NULL") == []
    assert desk.status(action.logical_action_id) is ActionStatus.AWAITING_STEPUP


def test_the_clock_never_moves_backwards(desk: Desk):
    desk.coordinator.advance_clock(EPOCH + timedelta(hours=1))
    with pytest.raises(WgError) as error:
        desk.coordinator.advance_clock(EPOCH)
    assert error.value.code is ErrorCode.UNSUPPORTED_REQUEST
    assert error.value.detail == {"reason": "clock_backwards"}


def test_advancing_to_now_changes_nothing(desk: Desk):
    audit_rows = desk.count("audit_log")
    state = desk.coordinator.advance_clock(EPOCH)
    assert state.now == EPOCH
    assert desk.count("audit_log") == audit_rows
    assert desk.rows("SELECT * FROM domain_events WHERE type = 'clock_advanced'") == []
