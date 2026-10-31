"""S5 direct debits under a customer lock; T104 at layers D and H.

The lock comes from the T21 corruption of the rent's unpaid amount on fixture_s5. Closing,
suspending and lowering an agreement and declining a request reduce risk and still work; paying,
raising a cap, resuming and creating an agreement are refused with LOCKED_WRITE and leave no
operation. A case passes on stored requests, agreements, operations and the ledger. Every S5
contract name is a string here, so the module imports on a build without S5.
"""

from __future__ import annotations

import pytest

from wg.contracts.api import ROUTES

from ..harness.client import MAIN_CUSTOMER, WgClient
from .common import active_locks
from .direct_debit import (
    AUTO_DECLINED,
    AWAITING,
    CAP_FEN,
    CLOUD,
    DECLINED,
    FITNESS,
    VIDEO,
    action_count,
    change_agreement,
    change_draft_response,
    confirm_card_response,
    debit_request,
    debit_requests,
    decision_draft_response,
    decline_request,
    draft_decision,
    effect_check,
    has_direct_debits,
    lock_customer,
    mandate,
    mandates,
    merchant_request,
    money_state,
    needs_changes,
    open_card,
    operation_count,
    payment_operations,
    refused,
    reset_s5,
)

pytestmark = pytest.mark.skipif(
    not has_direct_debits(), reason="the build lists no direct-debit routes"
)

LOCKED = (423, "LOCKED_WRITE")


def assert_still_locked(wg: WgClient) -> None:
    assert [(lock.scope, lock.scope_ref) for lock in active_locks(wg)] == [
        ("customer", MAIN_CUSTOMER)
    ]


# T104


@pytest.mark.case("T104")
@needs_changes
def test_t104_closing_lowering_suspending_and_declining_still_work(wg: WgClient) -> None:
    reset_s5(wg)
    video = merchant_request(wg, VIDEO, 2_500)
    cloud = merchant_request(wg, CLOUD, 2_100)
    lock_customer(wg)
    state = money_state(wg)

    declined = decline_request(wg, video["request_id"])
    assert declined["operation"] is None
    assert debit_request(wg, video["request_id"])["status"] == DECLINED

    lowered = change_agreement(wg, VIDEO, "set_cap", 2_000)
    assert lowered["draft"]["kind"] == "MANDATE_CAP_LOWER"
    assert mandate(wg, VIDEO)["cap_fen"] == 2_000

    suspended = change_agreement(wg, CLOUD, "suspend")
    assert mandate(wg, CLOUD)["status"] == "SUSPENDED"
    view = debit_request(wg, cloud["request_id"])
    assert (view["status"], view["reason_code"]) == (AUTO_DECLINED, "MANDATE_SUSPENDED")

    cancelled = change_agreement(wg, FITNESS, "cancel")
    assert mandate(wg, FITNESS)["status"] == "CANCELLED"

    # Each change settled with a receipt, and its probe ran although the customer is locked.
    for done in (lowered, suspended, cancelled):
        assert done["operation"]["status"] == "SETTLED"
        assert effect_check(wg, done["operation"]["operation_id"]) == "verified"
    # None of it moved money.
    assert money_state(wg)["posted"] == state["posted"]
    assert money_state(wg)["ledger"] == state["ledger"]
    assert payment_operations(wg) == []
    assert_still_locked(wg)


@pytest.mark.case("T104")
def test_t104_paying_is_refused_at_the_draft_and_at_an_open_card(wg: WgClient) -> None:
    reset_s5(wg)
    request = merchant_request(wg, CLOUD, 2_100)
    open_pay = draft_decision(wg, request["request_id"], "pay")
    card = open_card(wg, open_pay)
    lock_customer(wg)
    state, actions = money_state(wg), action_count(wg)

    refused(decision_draft_response(wg, request["request_id"], "pay"), *LOCKED)
    refused(confirm_card_response(wg, card), *LOCKED)

    assert action_count(wg) == actions
    assert payment_operations(wg) == [] and money_state(wg) == state
    view = debit_request(wg, request["request_id"])
    assert view["status"] == AWAITING and view["operation_id"] is None
    assert_still_locked(wg)


@pytest.mark.case("T104")
@needs_changes
def test_t104_raising_a_cap_is_refused(wg: WgClient) -> None:
    reset_s5(wg)
    lock_customer(wg)
    state, actions, operations = money_state(wg), action_count(wg), operation_count(wg)

    for ref, cap in ((FITNESS, 25_000), (VIDEO, 3_500)):
        refused(change_draft_response(wg, ref, "set_cap", cap), *LOCKED)

    assert action_count(wg) == actions and operation_count(wg) == operations
    assert money_state(wg) == state
    assert {m["mandate_ref"]: m["cap_fen"] for m in mandates(wg)["mandates"]} == {
        VIDEO: CAP_FEN[VIDEO],
        CLOUD: CAP_FEN[CLOUD],
        FITNESS: CAP_FEN[FITNESS],
    }
    assert_still_locked(wg)


@pytest.mark.case("T104")
@needs_changes
def test_t104_resuming_a_suspended_agreement_is_refused(wg: WgClient) -> None:
    reset_s5(wg, "video_suspended")
    lock_customer(wg)
    state, actions, operations = money_state(wg), action_count(wg), operation_count(wg)

    refused(change_draft_response(wg, VIDEO, "resume"), *LOCKED)

    assert action_count(wg) == actions and operation_count(wg) == operations
    assert mandate(wg, VIDEO)["status"] == "SUSPENDED" and money_state(wg) == state
    assert_still_locked(wg)


@pytest.mark.case("T104")
def test_t104_a_new_agreement_is_refused_or_not_built(wg: WgClient) -> None:
    reset_s5(wg)
    lock_customer(wg)
    state, actions, operations = money_state(wg), action_count(wg), operation_count(wg)
    request = {
        "merchant_id": "mer_music",
        "expected_fen": 1_500,
        "cap_fen": 2_000,
        "debit_day": 15,
        "months": 12,
    }

    response = wg.post("/v1/drafts/mandate", request)

    if any(r.method == "POST" and r.path == "/v1/drafts/mandate" for r in ROUTES):
        refused(response, *LOCKED)
    else:
        # Level 4 is not built: the route is absent (404, or 405 beside its sibling draft routes).
        assert response.status_code in (404, 405), response.text
    assert action_count(wg) == actions and operation_count(wg) == operations
    assert money_state(wg) == state
    assert len(mandates(wg)["mandates"]) == 3
    assert_still_locked(wg)


@pytest.mark.case("T104")
def test_t104_reads_and_reconciliation_keep_working_under_the_lock(wg: WgClient) -> None:
    reset_s5(wg)
    request = merchant_request(wg, VIDEO, 2_500)
    lock_customer(wg)

    assert len(mandates(wg)["mandates"]) == 3
    assert [r["request_id"] for r in debit_requests(wg)] == [request["request_id"]]
    assert debit_request(wg, request["request_id"])["status"] == AWAITING
    wg.snapshot()
    wg.reconcile(MAIN_CUSTOMER)
    assert_still_locked(wg)
