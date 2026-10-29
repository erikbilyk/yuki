"""A submission refused at the customer's own step must not run later without a new step.

Confirm and verify commit the approval (or the grant) and READY in one unit, then submit in a
second unit. When that submission is refused (INSUFFICIENT_FUNDS) or fails
(UNEXPECTED_FAILURE, "操作未执行"), the action stays READY with its approval valid until
the end of the simulated day. The next clock step or restart sends it. The customer has
already been told that it did not go through, so the money leaves later with no new step,
possibly next to a retry the customer made (SPEC §12.8, §15.9, §16).
"""

from __future__ import annotations

from wg.contracts.enums import Fault
from wg.contracts.errors import ErrorCode

from ..harness.client import WgClient

MOM = "pay_mom"
YELLOW_FEN = 50_000
RED_FEN = 200_000
# acc_main opens with 1,800,000 fen; this leaves 10,000 fen.
DRAIN_FEN = 1_790_000
TOP_UP_FEN = 3_000_000


def operations_to(wg: WgClient, payee_id: str) -> list[dict[str, object]]:
    return wg.db.rows(
        "SELECT operation_id, logical_action_id, amount_fen, status FROM operations"
        " WHERE payee_id = ?",
        (payee_id,),
    )


def test_refused_confirm_is_not_sent_by_a_later_clock_step(app_client: WgClient) -> None:
    wg = app_client
    draft = wg.draft_transfer(MOM, YELLOW_FEN)
    card = wg.open_card(draft.logical_action_id)
    wg.external_debit(DRAIN_FEN, "模拟商户")
    wg.refused(wg.confirm_response(card), ErrorCode.INSUFFICIENT_FUNDS)

    wg.incoming_payment(TOP_UP_FEN, "张三", "还款")
    wg.advance(minutes=1)

    assert wg.db.operations(draft.logical_action_id) == [], (
        "a transfer refused with INSUFFICIENT_FUNDS was sent by the next clock step"
    )


def test_refused_confirm_and_a_retry_send_one_transfer(app_client: WgClient) -> None:
    wg = app_client
    first = wg.draft_transfer(MOM, YELLOW_FEN)
    card = wg.open_card(first.logical_action_id)
    wg.external_debit(DRAIN_FEN, "模拟商户")
    wg.refused(wg.confirm_response(card), ErrorCode.INSUFFICIENT_FUNDS)

    # The customer tops up and sends the same transfer again.
    wg.incoming_payment(TOP_UP_FEN, "张三", "还款")
    wg.transfer(MOM, YELLOW_FEN)
    wg.advance(minutes=1)

    sent = operations_to(wg, MOM)
    assert len(sent) == 1, f"one intent, {len(sent)} transfers to 妈妈: {sent}"


def test_refused_verify_is_not_sent_by_a_later_clock_step(app_client: WgClient) -> None:
    wg = app_client
    draft = wg.draft_transfer(MOM, RED_FEN)
    confirm = wg.confirm(wg.open_card(draft.logical_action_id))
    assert confirm.challenge is not None
    wg.external_debit(DRAIN_FEN, "模拟商户")
    challenge_id = confirm.challenge.challenge_id
    response = wg.verify_response(challenge_id, wg.sms_code(challenge_id))
    wg.refused(response, ErrorCode.INSUFFICIENT_FUNDS)

    wg.incoming_payment(TOP_UP_FEN, "张三", "还款")
    wg.advance(minutes=1)

    assert wg.db.operations(draft.logical_action_id) == [], (
        "a RED transfer refused at verify was sent by the next clock step with its grant"
    )


def test_failed_confirm_is_not_sent_by_a_later_clock_step(app_client: WgClient) -> None:
    wg = app_client
    draft = wg.draft_transfer(MOM, YELLOW_FEN)
    card = wg.open_card(draft.logical_action_id)
    wg.arm(Fault.UNEXPECTED)
    wg.refused(wg.confirm_response(card), ErrorCode.UNEXPECTED_FAILURE)

    wg.advance(minutes=1)

    assert wg.db.operations(draft.logical_action_id) == [], (
        "a confirm answered with UNEXPECTED_FAILURE (操作未执行) was sent by the next clock step"
    )
