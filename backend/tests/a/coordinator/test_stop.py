"""Single plans, cancel and stop (SPEC §15.7, T12): unsubmitted nodes end, submitted ones settle."""

from datetime import timedelta

import pytest

from wg.contracts.enums import ActionStatus, PlanKind, PlanStatus, Tier
from wg.contracts.errors import ErrorCode, WgError
from wg.core import k4_approval as k4
from wg.core import k5_coordinator as k5
from wg.k6.simtime import now
from wg.services.actions import ActionService

from ..core.drafts import STEP_UP
from .conftest import DAD, EPOCH, Desk, Node

pytestmark = pytest.mark.layer("D")


def stop(desk: Desk, plan_id: str, customer_id: str = "cus_main"):
    return desk.db.run_in_uow(
        lambda uow: k5.stop_plan(
            uow, STEP_UP, customer_id=customer_id, plan_id=plan_id, at=now(uow)
        ),
        "test stop",
    )


def test_a_standalone_action_gets_its_own_single_plan_when_its_card_opens(desk: Desk):
    draft = desk.draft(10_000)
    assert draft.plan_id is None

    action = desk.confirmed(10_000)

    assert action.plan_id is not None
    (plan,) = desk.rows("SELECT * FROM plans WHERE plan_id = ?", action.plan_id)
    assert (plan["kind"], plan["customer_id"]) == (PlanKind.SINGLE.value, "cus_main")
    # The card shows no plan: the customer sees a single action.
    (card,) = desk.rows(
        "SELECT plan_id FROM cards WHERE logical_action_id = ?", action.logical_action_id
    )
    assert card["plan_id"] is None


def test_a_single_plan_follows_its_action(desk: Desk):
    action = desk.confirmed(10_000)
    desk.coordinator.tick()
    assert desk.status(action.logical_action_id) is ActionStatus.SETTLED
    assert desk.plan_status(action.plan_id) is PlanStatus.COMPLETED


@pytest.mark.case("T12")
def test_cancelling_a_waiting_action_ends_its_plan(desk: Desk):
    action = desk.confirmed(150_000)
    service = ActionService(desk.db, STEP_UP, desk.gateway)

    result = service.cancel("cus_main", action.logical_action_id)

    assert result.status is ActionStatus.CANCELLED
    assert desk.plan_status(action.plan_id) is PlanStatus.PARTIAL
    assert desk.rows("SELECT * FROM reservations") == []
    (challenge,) = desk.rows("SELECT * FROM challenges")
    assert challenge["invalidation_reason"] == "cancelled"


def test_stop_cancels_unsubmitted_nodes_and_keeps_submitted_ones(desk: Desk):
    plan = desk.plan(
        [
            Node("other_bank", 30_000, payee_id=DAD),
            Node("red", 150_000, tier=Tier.RED),
            Node("later", 10_000, not_before=EPOCH + timedelta(days=1)),
        ]
    )
    desk.coordinator.tick()
    assert desk.status(plan["other_bank"]) is ActionStatus.SUBMITTED
    assert desk.status(plan["red"]) is ActionStatus.AWAITING_STEPUP

    stopped = stop(desk, plan.plan_id)

    assert stopped.status is PlanStatus.PARTIAL
    assert desk.status(plan["red"]) is ActionStatus.CANCELLED
    assert desk.status(plan["later"]) is ActionStatus.CANCELLED
    assert desk.status(plan["other_bank"]) is ActionStatus.SUBMITTED
    # The submitted payment keeps its hold and settles at 09:00.
    assert len(desk.rows("SELECT * FROM reservations WHERE state = 'ACTIVE'")) == 1
    desk.coordinator.advance_clock(EPOCH + timedelta(days=1))
    assert desk.status(plan["other_bank"]) is ActionStatus.SETTLED
    assert desk.status(plan["later"]) is ActionStatus.CANCELLED
    assert desk.plan_status(plan.plan_id) is PlanStatus.PARTIAL


def test_stop_invalidates_open_codes_and_grants(desk: Desk):
    plan = desk.plan([Node("red", 150_000, tier=Tier.RED)])
    desk.coordinator.tick()
    action = desk.current(plan["red"])
    with desk.db.read() as conn:
        approval = k4.approval_for(conn, action)
    assert approval is not None
    desk.db.run_in_uow(
        lambda uow: k4.issue_challenge(uow, STEP_UP, action, approval, now(uow)), "test code"
    )

    stop(desk, plan.plan_id)

    (challenge,) = desk.rows("SELECT * FROM challenges")
    assert challenge["invalidation_reason"] == "plan_stopped"
    assert desk.rows("SELECT * FROM sms_outbox WHERE code IS NOT NULL") == []


def test_stop_is_owned_and_repeatable(desk: Desk):
    plan = desk.plan([Node("a", 10_000, not_before=EPOCH + timedelta(days=1))])
    with pytest.raises(WgError) as error:
        stop(desk, plan.plan_id, customer_id="cus_other")
    assert error.value.code is ErrorCode.NOT_FOUND

    first = stop(desk, plan.plan_id)
    again = stop(desk, plan.plan_id)
    assert first.status is again.status is PlanStatus.PARTIAL
    assert len(desk.rows("SELECT * FROM audit_log WHERE event = 'plan_stopped'")) == 1
