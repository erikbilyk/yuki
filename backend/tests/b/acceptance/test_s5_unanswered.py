"""S5 direct debits: a request nobody answers; T111 at layers D and H.

A request is answerable for 72 simulated hours. At `respond_by` it becomes EXPIRED, its period
stops being protected, and a new request of the same month is accepted again. A request the
customer declined keeps its period occupied and its protection until `respond_by`. Values are
S5-ACCEPTANCE §3.3; nothing is ever paid. A case passes on stored requests, audit rows, actions,
operations and the cash snapshot. Every S5 contract name is a string here, so the module imports
on a build without S5.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from ..harness.client import WgClient
from .direct_debit import (
    AWAITING,
    EPOCH,
    EXPIRED,
    OUT_OF_MANDATE,
    RESPOND_WITHIN,
    VIDEO,
    action_count,
    assert_period_rules,
    assert_subscriptions,
    at,
    audit_events,
    confirm_card_response,
    debit_request,
    decision_draft_response,
    decline_request,
    draft_decision,
    has_direct_debits,
    merchant_request,
    money_state,
    open_card,
    operation_count,
    outcome,
    payment_operations,
    refused,
    reset_s5,
)

pytestmark = pytest.mark.skipif(
    not has_direct_debits(), reason="the build lists no direct-debit routes"
)

RESPOND_BY = at(11, 5, 9, 0)
VIDEO_NOVEMBER = (VIDEO, "2026-11")


def assert_nothing_paid(wg: WgClient, state: dict[str, Any], operations: int) -> None:
    assert payment_operations(wg) == []
    assert money_state(wg) == state
    assert operation_count(wg) == operations


@pytest.mark.case("T111")
def test_t111_an_unanswered_request_expires_at_its_reply_time(wg: WgClient) -> None:
    reset_s5(wg)
    state, operations = money_state(wg), operation_count(wg)
    request = merchant_request(wg, VIDEO, 2_500)
    assert datetime.fromisoformat(request["respond_by"]) == EPOCH + RESPOND_WITHIN == RESPOND_BY
    assert_subscriptions(wg, 24_500)

    wg.advance(to=at(11, 5, 8, 59))

    waiting = debit_request(wg, request["request_id"])
    assert waiting["status"] == AWAITING and waiting["payable"] is True
    assert VIDEO_NOVEMBER in assert_subscriptions(wg, 27_000)

    wg.advance(to=RESPOND_BY)

    expired = debit_request(wg, request["request_id"])
    assert (expired["status"], expired["reason_code"]) == (EXPIRED, "EXPIRED")
    assert expired["payable"] is False and expired["operation_id"] is None
    assert VIDEO_NOVEMBER not in assert_subscriptions(wg, 24_500)
    assert_nothing_paid(wg, state, operations)

    wg.advance(to=at(11, 6, 9, 0))

    again = merchant_request(wg, VIDEO, 2_500)
    assert outcome(again) == (AWAITING, None), again
    assert debit_request(wg, again["request_id"])["period_key"] == "2026-11"
    assert VIDEO_NOVEMBER in assert_subscriptions(wg, 27_000)
    assert debit_request(wg, request["request_id"])["status"] == EXPIRED
    assert_nothing_paid(wg, state, operations)
    assert_period_rules(wg)


@pytest.mark.case("T111")
def test_t111_the_expiry_is_recorded_at_the_reply_time_after_a_long_jump(wg: WgClient) -> None:
    reset_s5(wg)
    request = merchant_request(wg, VIDEO, 2_500)

    wg.advance(to=at(11, 6, 9, 0))

    rows = [r for r in audit_events(wg, "request_expired") if r["ref"] == request["request_id"]]
    assert len(rows) == 1, rows
    assert datetime.fromisoformat(rows[0]["at"]) == RESPOND_BY
    assert debit_request(wg, request["request_id"])["status"] == EXPIRED
    assert payment_operations(wg) == []


@pytest.mark.case("T111")
def test_t111_an_expired_request_can_neither_be_paid_nor_declined(wg: WgClient) -> None:
    reset_s5(wg)
    request = merchant_request(wg, VIDEO, 2_500)
    wg.advance(to=at(11, 5, 9, 1))
    state, actions, operations = money_state(wg), action_count(wg), operation_count(wg)

    for decision in ("pay", "decline"):
        response = decision_draft_response(wg, request["request_id"], decision)
        refused(response, 409, "REQUEST_NOT_PAYABLE")

    assert (action_count(wg), operation_count(wg)) == (actions, operations)
    assert_nothing_paid(wg, state, operations)


@pytest.mark.case("T111")
def test_t111_a_card_open_at_the_reply_time_cannot_be_confirmed(wg: WgClient) -> None:
    reset_s5(wg)
    request = merchant_request(wg, VIDEO, 2_500)
    wg.advance(to=at(11, 5, 8, 59))
    state, operations = money_state(wg), operation_count(wg)
    card = open_card(wg, draft_decision(wg, request["request_id"], "pay"))

    wg.advance(minutes=1)

    refused(confirm_card_response(wg, card), 409, "APPROVAL_EXPIRED")
    assert debit_request(wg, request["request_id"])["status"] == EXPIRED
    assert_nothing_paid(wg, state, operations)


@pytest.mark.case("T111")
def test_t111_a_declined_request_keeps_its_period_and_protection_until_the_reply_time(
    wg: WgClient,
) -> None:
    reset_s5(wg)
    request = merchant_request(wg, VIDEO, 2_500)
    assert decline_request(wg, request["request_id"])["operation"] is None
    state, operations = money_state(wg), operation_count(wg)
    assert VIDEO_NOVEMBER in assert_subscriptions(wg, 24_500)

    wg.advance(to=at(11, 5, 8, 59))

    assert VIDEO_NOVEMBER in assert_subscriptions(wg, 27_000)

    wg.advance(to=RESPOND_BY)

    assert VIDEO_NOVEMBER not in assert_subscriptions(wg, 24_500)
    declined = debit_request(wg, request["request_id"])
    assert declined["status"] == "DECLINED" and declined["reason_code"] is None
    assert [
        r for r in audit_events(wg, "request_expired") if r["ref"] == request["request_id"]
    ] == []

    wg.advance(to=at(11, 6, 9, 0))

    later = merchant_request(wg, VIDEO, 2_500)
    assert outcome(later) == (OUT_OF_MANDATE, "PERIOD_USED"), later
    assert VIDEO_NOVEMBER not in assert_subscriptions(wg, 24_500)
    assert_nothing_paid(wg, state, operations)
    assert_period_rules(wg)
