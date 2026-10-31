"""S5 direct debits: pay against change, and cards made stale; T103 at layers D and H.

A payment card carries the agreement's version and the request it pays. Cancelling, lowering the
cap or answering the request in another way closes the card, and confirming it afterwards is
refused with no operation. A case passes on stored requests, actions, operations and the ledger.
Every S5 contract name is a string here, so the module imports on a build without S5.
"""

from __future__ import annotations

import json

import pytest

from ..harness.client import WgClient
from .direct_debit import (
    AUTO_DECLINED,
    AWAITING,
    DECLINED,
    OPEN_ACTION_STATUSES,
    OUT_OF_MANDATE,
    PAID,
    VIDEO,
    assert_paid_once,
    body,
    change_agreement,
    complete,
    confirm_card_response,
    debit_request,
    debit_requests,
    decision_draft_response,
    draft_change,
    draft_decision,
    effect_check,
    effect_events,
    has_direct_debits,
    mandate,
    mandate_events,
    merchant_request,
    money_state,
    open_card,
    operation_kinds,
    operations_of,
    payment_operations,
    probes,
    race,
    refused,
    reset_s5,
)

pytestmark = pytest.mark.skipif(
    not has_direct_debits(), reason="the build lists no direct-debit routes"
)

# Why a payment can be refused once the agreement or the request has moved on.
PAYMENT_REFUSALS = ("APPROVAL_EXPIRED", "REQUEST_NOT_PAYABLE", "MANDATE_CHANGED")
CONFIRMED_STATUSES = (200, 201, 202)


def card_status(wg: WgClient, card: dict) -> str:
    return body(wg.get(f"/v1/cards/{card['card_id']}"))["status"]


def assert_stale_card_refused(wg: WgClient, card: dict, logical_action_id: str) -> None:
    """The card shows expired, its action is CANCELLED and confirming it moves nothing."""
    state = money_state(wg)
    assert card_status(wg, card) == "expired"
    assert wg.db.action_status(logical_action_id) == "CANCELLED"
    refused(confirm_card_response(wg, card), 409, "APPROVAL_EXPIRED")
    assert payment_operations(wg) == []
    assert money_state(wg) == state


# T103


@pytest.mark.case("T103")
@pytest.mark.parametrize("attempt", [1, 2, 3, 4])
def test_t103_pay_and_cancel_confirmed_in_parallel_leave_at_most_one_effect(
    wg: WgClient, attempt: int
) -> None:
    reset_s5(wg)
    request = merchant_request(wg, VIDEO, 2_500)
    pay = draft_decision(wg, request["request_id"], "pay")
    pay_card = open_card(wg, pay)
    cancel = draft_change(wg, VIDEO, "cancel")
    cancel_card = open_card(wg, cancel)
    start = money_state(wg)["posted"]

    pay_response, cancel_response = race(
        wg,
        [
            lambda tab: confirm_card_response(tab, pay_card),
            lambda tab: confirm_card_response(tab, cancel_card),
        ],
    )

    # The cancel always goes through; the payment either settled first or was refused.
    assert cancel_response.status_code in CONFIRMED_STATUSES, cancel_response.text
    assert mandate(wg, VIDEO)["status"] == "CANCELLED"
    assert len(operations_of(wg, "MANDATE_CANCEL")) == 1
    view = debit_request(wg, request["request_id"])
    kinds = operation_kinds(wg)
    if view["status"] == PAID:
        assert pay_response.status_code in CONFIRMED_STATUSES, pay_response.text
        assert_paid_once(wg, request["request_id"], 2_500)
        # Paid, then cancelled: the payment came first.
        assert kinds.index("DEBIT_PAY") < kinds.index("MANDATE_CANCEL")
        assert wg.balance().posted_fen == start - 2_500
    else:
        assert (view["status"], view["reason_code"]) == (AUTO_DECLINED, "MANDATE_CANCELLED")
        assert pay_response.status_code == 409, pay_response.text
        assert pay_response.json()["code"] in PAYMENT_REFUSALS, pay_response.text
        assert payment_operations(wg) == []
        assert wg.balance().posted_fen == start
    # Nothing is left pending: no waiting request, no hold, no action still open.
    assert not [r for r in debit_requests(wg) if r["status"] == AWAITING]
    assert wg.db.reservations("ACTIVE") == []
    for action in (pay, cancel):
        assert wg.db.action_status(action["logical_action_id"]) not in OPEN_ACTION_STATUSES


@pytest.mark.case("T103")
def test_t103_lowering_the_cap_below_the_request_closes_the_payment_card(wg: WgClient) -> None:
    reset_s5(wg)
    request = merchant_request(wg, VIDEO, 2_500)
    pay = draft_decision(wg, request["request_id"], "pay")
    card = open_card(wg, pay)
    before = mandate(wg, VIDEO)

    lowered = change_agreement(wg, VIDEO, "set_cap", 2_000)

    after = mandate(wg, VIDEO)
    assert after["cap_fen"] == 2_000 and after["version"] == before["version"] + 1
    assert lowered["draft"]["kind"] == "MANDATE_CAP_LOWER" and lowered["verify"] is None
    view = debit_request(wg, request["request_id"])
    assert (view["status"], view["reason_code"]) == (OUT_OF_MANDATE, "CAP")
    assert view["payable"] is False and view["operation_id"] is None
    assert_stale_card_refused(wg, card, pay["logical_action_id"])
    refused(decision_draft_response(wg, request["request_id"], "pay"), 409, "REQUEST_NOT_PAYABLE")
    assert debit_request(wg, request["request_id"])["status"] == OUT_OF_MANDATE
    # The change is checked with a probe one fen above the new cap.
    operation_id = lowered["operation"]["operation_id"]
    (probe,) = probes(wg, VIDEO)
    assert probe["amount_fen"] == 2_001
    assert (probe["status"], probe["reason_code"]) == (OUT_OF_MANDATE, "CAP")
    assert [e["check_kind"] for e in effect_events(wg, operation_id)] == ["probe_cap"]
    assert effect_check(wg, operation_id) == "verified"
    cap_events = [e for e in mandate_events(wg, VIDEO) if e["field"] == "cap_fen"]
    assert [(str(e["old"]), str(e["new"])) for e in cap_events] == [("3000", "2000")]


@pytest.mark.case("T103")
def test_t103_a_cap_that_still_covers_the_request_changes_the_card_not_the_request(
    wg: WgClient,
) -> None:
    reset_s5(wg)
    request = merchant_request(wg, VIDEO, 2_500)
    pay = draft_decision(wg, request["request_id"], "pay")
    old_card = open_card(wg, pay)
    before = mandate(wg, VIDEO)

    change_agreement(wg, VIDEO, "set_cap", 2_800)

    # The request stays payable, but the card was made for the old agreement version.
    after = mandate(wg, VIDEO)
    assert after["cap_fen"] == 2_800 and after["version"] == before["version"] + 1
    view = debit_request(wg, request["request_id"])
    assert view["status"] == AWAITING and view["payable"] is True
    assert_stale_card_refused(wg, old_card, pay["logical_action_id"])

    fresh = draft_decision(wg, request["request_id"], "pay")
    assert fresh["logical_action_id"] != pay["logical_action_id"]
    assert fresh["kind"] == "DEBIT_PAY" and fresh["feasible"] is True
    old_params = json.loads(wg.db.actions(pay["logical_action_id"])[-1]["params_json"])
    new_params = json.loads(wg.db.actions(fresh["logical_action_id"])[-1]["params_json"])
    assert old_params["mandate_version"] == before["version"]
    assert new_params["mandate_version"] == after["version"]
    new_card = open_card(wg, fresh)
    assert new_card["terms_hash"] != old_card["terms_hash"]

    paid = complete(wg, fresh, new_card)

    assert paid["operation"]["status"] == "SETTLED"
    assert_paid_once(wg, request["request_id"], 2_500)


@pytest.mark.case("T103")
def test_t103_a_changed_agreement_refuses_a_stale_card_even_when_the_request_is_waiting(
    wg: WgClient,
) -> None:
    reset_s5(wg)
    request = merchant_request(wg, VIDEO, 2_500)
    pay = draft_decision(wg, request["request_id"], "pay")
    card = open_card(wg, pay)

    change_agreement(wg, VIDEO, "suspend")

    view = debit_request(wg, request["request_id"])
    assert (view["status"], view["reason_code"]) == (AUTO_DECLINED, "MANDATE_SUSPENDED")
    assert_stale_card_refused(wg, card, pay["logical_action_id"])


@pytest.mark.case("T103")
def test_t103_answering_a_request_closes_the_card_of_the_other_answer(wg: WgClient) -> None:
    reset_s5(wg)
    request = merchant_request(wg, VIDEO, 2_500)
    pay = draft_decision(wg, request["request_id"], "pay")
    pay_card = open_card(wg, pay)
    decline = draft_decision(wg, request["request_id"], "decline")
    decline_card = open_card(wg, decline)

    declined = complete(wg, decline, decline_card)

    assert declined["operation"] is None
    assert debit_request(wg, request["request_id"])["status"] == DECLINED
    assert_stale_card_refused(wg, pay_card, pay["logical_action_id"])
    refused(decision_draft_response(wg, request["request_id"], "pay"), 409, "REQUEST_NOT_PAYABLE")
