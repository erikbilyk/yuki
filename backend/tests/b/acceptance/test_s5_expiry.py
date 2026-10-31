"""S5 direct debits: requests to agreements that no longer authorize them; T102 at layers D and H.

Merchant requests arrive after a cancel, while an agreement is suspended and after its
`expires_on`; one clock advance from the epoch crosses the expiry. A case passes on stored
requests, agreement events, operations and the ledger. Every S5 contract name is a string here,
so the module imports on a build without S5.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from ..harness.client import WgClient
from .direct_debit import (
    AUTO_DECLINED,
    AWAITING,
    EXPIRED,
    FITNESS,
    VIDEO,
    at,
    change_agreement,
    debit_request,
    debit_requests,
    decision_draft_response,
    has_direct_debits,
    mandate,
    mandate_events,
    mandates,
    merchant_request,
    money_state,
    operation_count,
    outcome,
    payment_operations,
    refused,
    reset_s5,
    stored_mandate,
)

pytestmark = pytest.mark.skipif(
    not has_direct_debits(), reason="the build lists no direct-debit routes"
)


# T102


@pytest.mark.case("T102")
def test_t102_a_request_after_the_cancel_is_declined(wg: WgClient) -> None:
    reset_s5(wg)
    change_agreement(wg, FITNESS, "cancel")
    operations, state = operation_count(wg), money_state(wg)

    within = merchant_request(wg, FITNESS, 19_900)
    above = merchant_request(wg, FITNESS, 22_001)

    # The agreement is checked before the cap: both are declined for the same reason.
    assert outcome(within) == (AUTO_DECLINED, "MANDATE_CANCELLED")
    assert outcome(above) == (AUTO_DECLINED, "MANDATE_CANCELLED")
    listed = {r["request_id"]: r for r in debit_requests(wg)}
    assert set(listed) == {within["request_id"], above["request_id"]}
    assert all(r["payable"] is False and r["status"] == AUTO_DECLINED for r in listed.values())
    for request_id in listed:
        refused(decision_draft_response(wg, request_id, "pay"), 409, "REQUEST_NOT_PAYABLE")
    assert operation_count(wg) == operations and money_state(wg) == state
    assert payment_operations(wg) == []
    assert mandate(wg, FITNESS)["status"] == "CANCELLED"


@pytest.mark.case("T102")
def test_t102_a_request_while_the_agreement_is_suspended_is_declined(wg: WgClient) -> None:
    reset_s5(wg, "video_suspended")
    assert mandate(wg, VIDEO)["status"] == "SUSPENDED"
    operations, state = operation_count(wg), money_state(wg)

    result = merchant_request(wg, VIDEO, 2_500)

    assert outcome(result) == (AUTO_DECLINED, "MANDATE_SUSPENDED")
    view = debit_request(wg, result["request_id"])
    assert view["payable"] is False and view["operation_id"] is None
    assert operation_count(wg) == operations and money_state(wg) == state


@pytest.mark.case("T102")
def test_t102_a_suspend_declines_waiting_requests_and_later_ones(wg: WgClient) -> None:
    reset_s5(wg)
    waiting = merchant_request(wg, VIDEO, 2_500)
    assert waiting["status"] == AWAITING

    done = change_agreement(wg, VIDEO, "suspend")
    assert done["operation"]["status"] == "SETTLED"
    state = money_state(wg)

    assert mandate(wg, VIDEO)["status"] == "SUSPENDED"
    view = debit_request(wg, waiting["request_id"])
    assert (view["status"], view["reason_code"]) == (AUTO_DECLINED, "MANDATE_SUSPENDED")
    later = merchant_request(wg, VIDEO, 2_500)
    assert outcome(later) == (AUTO_DECLINED, "MANDATE_SUSPENDED")
    assert money_state(wg) == state and payment_operations(wg) == []

    # Only the customer's confirmed resume, with its code, lets requests in again.
    resumed = change_agreement(wg, VIDEO, "resume")
    assert resumed["verify"] is not None and resumed["card"]["requires_code"] is True
    assert mandate(wg, VIDEO)["status"] == "ACTIVE"
    assert outcome(merchant_request(wg, VIDEO, 2_500)) == (AWAITING, None)
    assert payment_operations(wg) == []


@pytest.mark.case("T102")
def test_t102_a_request_after_the_agreement_expired_is_declined(wg: WgClient) -> None:
    reset_s5(wg, "video_expiring")
    wg.advance(to=at(11, 6, 0, 0))
    assert mandate(wg, VIDEO)["status"] == "EXPIRED"
    operations, state = operation_count(wg), money_state(wg)

    result = merchant_request(wg, VIDEO, 2_500)

    assert outcome(result) == (AUTO_DECLINED, "MANDATE_EXPIRED")
    assert operation_count(wg) == operations and money_state(wg) == state
    assert mandate(wg, VIDEO)["expires_on"] == "2026-11-05"


@pytest.mark.case("T102")
def test_t102_a_request_a_minute_before_the_expiry_is_still_accepted(wg: WgClient) -> None:
    reset_s5(wg, "video_expiring")
    assert mandate(wg, VIDEO)["expires_on"] == "2026-11-05"
    wg.advance(to=at(11, 5, 23, 59))

    result = merchant_request(wg, VIDEO, 2_500)

    assert outcome(result) == (AWAITING, None)
    assert mandate(wg, VIDEO)["status"] == "ACTIVE"
    assert debit_request(wg, result["request_id"])["payable"] is True


def assert_expired_at_midnight(wg: WgClient, events_before: int) -> None:
    """The agreement ended at its expiry instant, not at the end of the clock jump."""
    assert mandate(wg, VIDEO)["status"] == "EXPIRED"
    assert mandate(wg, VIDEO)["next_debit_date"] is None
    assert mandate(wg, VIDEO)["expires_on"] == "2026-11-05"
    assert stored_mandate(wg, VIDEO)["expires_on"] == "2026-11-05"
    (event,) = mandate_events(wg, VIDEO)[events_before:]
    assert (event["field"], event["old"], event["new"]) == ("status", "ACTIVE", "EXPIRED")
    assert event["operation_id"] is None
    assert (event["actor"], event["reason"]) == ("coordinator", "expires_on")
    assert datetime.fromisoformat(event["at"]) == at(11, 6, 0, 0)
    assert date.fromisoformat(stored_mandate(wg, VIDEO)["expires_on"]) == date(2026, 11, 5)


@pytest.mark.case("T102")
def test_t102_one_clock_advance_past_the_expiry_stops_at_the_expiry_instant(
    wg: WgClient,
) -> None:
    reset_s5(wg, "video_expiring")
    events_before = len(mandate_events(wg, VIDEO))
    operations = operation_count(wg)

    wg.advance(to=at(11, 10, 9, 0))

    assert_expired_at_midnight(wg, events_before)
    result = merchant_request(wg, VIDEO, 2_500)
    assert outcome(result) == (AUTO_DECLINED, "MANDATE_EXPIRED")
    assert operation_count(wg) == operations and payment_operations(wg) == []
    # The other agreements are untouched by the jump.
    assert [m["status"] for m in mandates(wg)["mandates"] if m["mandate_ref"] != VIDEO] == [
        "ACTIVE",
        "ACTIVE",
    ]


@pytest.mark.case("T102")
def test_t102_a_request_waiting_at_the_expiry_is_declined_with_it(wg: WgClient) -> None:
    reset_s5(wg, "video_expiring")
    events_before = len(mandate_events(wg, VIDEO))
    wg.advance(to=at(11, 5, 23, 0))
    waiting = merchant_request(wg, VIDEO, 2_500)
    assert waiting["status"] == AWAITING

    # The reply time of the request (11-08 23:00) lies after the expiry (11-06 00:00).
    wg.advance(to=at(11, 10, 9, 0))

    assert_expired_at_midnight(wg, events_before)
    view = debit_request(wg, waiting["request_id"])
    assert (view["status"], view["reason_code"]) == (AUTO_DECLINED, "MANDATE_EXPIRED")
    assert view["status"] != EXPIRED and view["payable"] is False
    assert payment_operations(wg) == []
    refused(decision_draft_response(wg, waiting["request_id"], "pay"), 409, "REQUEST_NOT_PAYABLE")
