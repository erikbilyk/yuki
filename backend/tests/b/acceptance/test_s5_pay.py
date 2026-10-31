"""S5 direct debits: T98 at layers D and H (S5-ACCEPTANCE §4, golden values of §3.3).

The customer pays a merchant's request on the guided path: draft, card, confirm. A case passes on
the operation, its ledger entries and receipt, the request row and the cash snapshot. Every S5
contract name is a string here, so the module imports on a build without S5.
"""

from __future__ import annotations

from datetime import date

import pytest

from ..harness.client import WgClient
from .common import assert_cash
from .direct_debit import (
    AWAITING,
    CLOUD,
    FITNESS,
    PAID,
    VIDEO,
    approve,
    assert_paid_once,
    assert_subscriptions,
    at,
    complete,
    debit_request,
    draft_decision,
    has_direct_debits,
    mandate,
    merchant_request,
    money_state,
    open_card,
    pay_request,
    payment_operations,
    reset_s5,
    stored_requests,
    subscription_periods,
    subscription_spend,
)

pytestmark = pytest.mark.skipif(
    not has_direct_debits(), reason="the build lists no direct-debit routes"
)

MOM = "pay_mom"
START_C = 1_800_000
START_PROTECTED = 1_624_500
START_HEADROOM = 175_500


def assert_start(wg: WgClient) -> None:
    """S5-ACCEPTANCE §3.1 at the epoch: B holds the bills and the three November periods."""
    assert_cash(wg, c=START_C, protected=START_PROTECTED, headroom=START_HEADROOM)
    assert_subscriptions(wg, 24_500)


# T98


@pytest.mark.case("T98")
def test_t98_the_customer_pays_a_request_at_the_expected_amount(wg: WgClient) -> None:
    reset_s5(wg)
    assert_start(wg)
    request = merchant_request(wg, VIDEO, 2_500)
    assert request["status"] == AWAITING
    assert debit_request(wg, request["request_id"])["payable"] is True
    # A waiting request holds nothing.
    assert_start(wg)
    assert payment_operations(wg) == []

    draft = draft_decision(wg, request["request_id"], "pay")
    assert (draft["kind"], draft["tier"], draft["feasible"]) == ("DEBIT_PAY", "YELLOW", True)
    paid = approve(wg, draft)

    assert paid["verify"] is None and paid["confirm"]["challenge"] is None
    operation = paid["operation"]
    assert operation is not None and operation["status"] == "SETTLED"
    stored = assert_paid_once(wg, request["request_id"], 2_500)
    assert operation["operation_id"] == stored["operation_id"]
    assert wg.db.reservations("ACTIVE") == []

    # C and B fall together; the free money does not move.
    assert_cash(wg, c=1_797_500, protected=1_622_000, headroom=START_HEADROOM)
    periods = assert_subscriptions(wg, 22_000)
    assert (VIDEO, "2026-11") not in periods
    assert wg.balance().posted_fen == 1_797_500
    assert mandate(wg, VIDEO)["expected_fen"] == 2_500

    spend = subscription_spend(wg)
    assert spend["total_fen"] == 2_500
    # One row per agreement, zero counts included; only the video row has a payment.
    rows = {r["mandate_ref"]: r for r in spend["rows"]}
    assert set(rows) == {CLOUD, FITNESS, VIDEO}
    video = rows[VIDEO]
    assert (video["paid_count"], video["paid_fen"], video["last_amount_fen"]) == (1, 2_500, 2_500)
    assert date.fromisoformat(video["last_paid_on"]) == date(2026, 11, 2)
    for ref in (CLOUD, FITNESS):
        assert (rows[ref]["paid_count"], rows[ref]["paid_fen"]) == (0, 0)
        assert rows[ref]["last_paid_on"] is None and rows[ref]["last_amount_fen"] is None
    # The customer's transfer history is not a list of merchant payments.
    assert wg.history().transfers == ()


@pytest.mark.case("T98")
def test_t98_a_request_above_the_expected_amount_draws_on_headroom(wg: WgClient) -> None:
    reset_s5(wg)
    request = merchant_request(wg, VIDEO, 3_000)
    draft = draft_decision(wg, request["request_id"], "pay")
    assert (draft["kind"], draft["tier"], draft["feasible"]) == ("DEBIT_PAY", "YELLOW", True)
    card = open_card(wg, draft)
    assert card["status"] == "open"
    assert any("本次金额高于每月预计金额" in w for w in card["warnings"]), card["warnings"]

    paid = complete(wg, draft, card)

    assert paid["operation"]["status"] == "SETTLED"
    assert_paid_once(wg, request["request_id"], 3_000)
    # The 500 above the expected 2,500 came from headroom.
    assert_cash(wg, c=1_797_000, protected=1_622_000, headroom=175_000)
    assert_subscriptions(wg, 22_000)
    assert mandate(wg, VIDEO)["expected_fen"] == 3_000

    # From 11-05 00:00 the next period is protected at the amount last paid.
    wg.advance(to=at(11, 5, 0, 0))
    periods = subscription_periods(wg)
    assert periods[(VIDEO, "2026-12")]["expected_fen"] == 3_000
    assert (VIDEO, "2026-11") not in periods
    assert [r["status"] for r in stored_requests(wg, VIDEO)] == [PAID]


@pytest.mark.case("T98")
def test_t98_a_payment_counts_in_the_day_sum_after_a_transfer(wg: WgClient) -> None:
    reset_s5(wg)
    wg.transfer(MOM, 97_500)
    request = merchant_request(wg, VIDEO, 2_500)

    # 97,500 + 2,500 = 100,000, the YELLOW limit, inclusive.
    draft = draft_decision(wg, request["request_id"], "pay")
    assert (draft["kind"], draft["tier"]) == ("DEBIT_PAY", "YELLOW")
    paid = approve(wg, draft)
    assert paid["verify"] is None
    assert_paid_once(wg, request["request_id"], 2_500)


@pytest.mark.case("T98")
def test_t98_one_yuan_over_the_day_limit_makes_the_payment_red(wg: WgClient) -> None:
    reset_s5(wg)
    wg.transfer(MOM, 99_000)
    request = merchant_request(wg, VIDEO, 2_500)

    # 99,000 + 2,500 = 101,500: RED, and only the code executes it.
    draft = draft_decision(wg, request["request_id"], "pay")
    assert (draft["kind"], draft["tier"]) == ("DEBIT_PAY", "RED")
    paid = approve(wg, draft)
    assert paid["confirm"]["challenge"] is not None and paid["verify"] is not None
    assert paid["card"]["requires_code"] is True
    assert_paid_once(wg, request["request_id"], 2_500)


@pytest.mark.case("T98")
def test_t98_a_payment_counts_in_the_day_sum_before_a_transfer(wg: WgClient) -> None:
    reset_s5(wg)
    request = merchant_request(wg, VIDEO, 2_500)
    pay_request(wg, request["request_id"])
    assert_paid_once(wg, request["request_id"], 2_500)

    exactly = wg.draft_transfer(MOM, 97_500)
    over = wg.draft_transfer(MOM, 97_501)

    assert (exactly.tier.value, over.tier.value) == ("YELLOW", "RED")


@pytest.mark.case("T98")
def test_t98_a_second_payment_attempt_for_the_same_request_is_refused(wg: WgClient) -> None:
    reset_s5(wg)
    request = merchant_request(wg, CLOUD, 2_100)
    pay_request(wg, request["request_id"])
    state = money_state(wg)

    response = wg.post(
        "/v1/drafts/debit-decision", {"request_id": request["request_id"], "decision": "pay"}
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "REQUEST_NOT_PAYABLE"
    assert response.json()["detail"]["request_id"] == request["request_id"]
    assert response.json()["detail"]["reason"] == "request_not_awaiting"
    assert money_state(wg) == state
    assert len(payment_operations(wg)) == 1
    assert debit_request(wg, request["request_id"])["status"] == PAID
