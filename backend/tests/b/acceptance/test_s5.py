"""S5 direct debits: T99 and T100 at layers D and H (S5-ACCEPTANCE §4).

Merchant requests arrive through the simulator (POST /sim/debit-requests); the customer reads and
pays through the v1 routes. A case passes on stored requests, actions, operations and audit rows.
Every S5 contract name is a string here: the module imports on a build without S5 and its
cases are skipped there.

Agreement mdt_video (expected 2,500, cap 3,000, debit day 5) carries most variants; mdt_cloud
(expected and cap 2,100) is used where a request must exceed the cap by a margin.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

import pytest

from ..harness.client import MAIN_CUSTOMER, WgClient, describe
from .direct_debit import (
    AWAITING,
    CAP_FEN,
    CLOUD,
    DECLINED,
    EPOCH,
    FITNESS,
    OUT_OF_MANDATE,
    PAID,
    RESPOND_WITHIN,
    VIDEO,
    action_count,
    assert_period_rules,
    at,
    audit_events,
    debit_request,
    debit_requests,
    decision_draft_response,
    decline_request,
    has_direct_debits,
    merchant_request,
    merchant_request_response,
    operation_count,
    pay_request,
    race,
    refused,
    reset_s5,
    stored_requests,
)

pytestmark = pytest.mark.skipif(
    not has_direct_debits(), reason="the build lists no direct-debit routes"
)

MOM = "pay_mom"
CAP = CAP_FEN[VIDEO]


def outcome(result: dict[str, Any]) -> tuple[str, str | None]:
    """Status and reason code of a SimDebitResult."""
    return result["status"], result["reason_code"]


# T99


@pytest.mark.case("T99")
def test_t99_a_request_at_the_cap_is_accepted_and_one_fen_above_is_not(wg: WgClient) -> None:
    reset_s5(wg)
    operations = operation_count(wg)
    at_cap = merchant_request(wg, VIDEO, CAP, request_id="m-at-cap")
    above = merchant_request(wg, VIDEO, CAP + 1, request_id="m-above-cap")

    assert outcome(at_cap) == (AWAITING, None) and at_cap["duplicate"] is False
    assert outcome(above) == (OUT_OF_MANDATE, "CAP")
    for result in (at_cap, above):
        assert datetime.fromisoformat(result["respond_by"]) == EPOCH + RESPOND_WITHIN
    views = {r["request_id"]: r for r in debit_requests(wg)}
    assert views[at_cap["request_id"]]["amount_fen"] == CAP
    assert views[at_cap["request_id"]]["payable"] is True
    assert views[above["request_id"]]["amount_fen"] == CAP + 1
    assert views[above["request_id"]]["payable"] is False
    assert operation_count(wg) == operations
    assert_period_rules(wg)


@pytest.mark.case("T99")
def test_t99_the_order_of_the_two_requests_does_not_matter(wg: WgClient) -> None:
    reset_s5(wg)
    above = merchant_request(wg, VIDEO, CAP + 1, request_id="m-above-cap")
    at_cap = merchant_request(wg, VIDEO, CAP, request_id="m-at-cap")

    # An out-of-mandate request does not use the period.
    assert outcome(above) == (OUT_OF_MANDATE, "CAP")
    assert outcome(at_cap) == (AWAITING, None)
    assert sorted(r["status"] for r in stored_requests(wg, VIDEO)) == [AWAITING, OUT_OF_MANDATE]
    assert_period_rules(wg)


@pytest.mark.case("T99")
def test_t99_a_request_above_the_cap_cannot_be_drafted_for_payment_or_decline(
    wg: WgClient,
) -> None:
    reset_s5(wg)
    above = merchant_request(wg, VIDEO, CAP + 1)
    actions, operations = action_count(wg), operation_count(wg)

    for decision in ("pay", "decline"):
        error = refused(
            decision_draft_response(wg, above["request_id"], decision), 409, "REQUEST_NOT_PAYABLE"
        )
        assert error["detail"]["request_id"] == above["request_id"]
        assert error["detail"]["reason"] == "request_not_awaiting"

    assert action_count(wg) == actions and operation_count(wg) == operations
    assert debit_request(wg, above["request_id"])["status"] == OUT_OF_MANDATE


@pytest.mark.case("T99")
def test_t99_a_request_at_the_cap_can_be_drafted_for_payment(wg: WgClient) -> None:
    reset_s5(wg)
    at_cap = merchant_request(wg, VIDEO, CAP)
    actions, operations = action_count(wg), operation_count(wg)

    response = decision_draft_response(wg, at_cap["request_id"], "pay")
    assert response.status_code == 201, describe(response)
    draft = response.json()
    assert draft["kind"] == "DEBIT_PAY" and draft["tier"] == "YELLOW"
    assert draft["feasible"] is True and draft["reasons"] == []
    assert action_count(wg) == actions + 1
    assert operation_count(wg) == operations


@pytest.mark.case("T99")
def test_t99_the_transfer_route_has_no_merchant_or_request_field(wg: WgClient) -> None:
    reset_s5(wg)
    request = merchant_request(wg, VIDEO, CAP + 1)
    actions, operations = action_count(wg), operation_count(wg)

    for extra in (
        {"merchant_id": "mer_video"},
        {"request_id": request["request_id"]},
        {"mandate_ref": VIDEO},
    ):
        response = wg.post("/v1/drafts/transfer", {"payee_id": MOM, "amount_fen": 100, **extra})
        assert response.status_code == 422, describe(response)

    assert action_count(wg) == actions and operation_count(wg) == operations
    assert debit_request(wg, request["request_id"])["status"] == OUT_OF_MANDATE


@pytest.mark.case("T99")
@pytest.mark.parametrize("amount", [0, -500], ids=["zero", "negative"])
def test_t99_a_non_positive_amount_is_refused_audited_and_not_stored(
    wg: WgClient, amount: int
) -> None:
    reset_s5(wg)
    rows_before = len(stored_requests(wg))
    refusals_before = len(audit_events(wg, "debit_request_refused"))

    error = refused(merchant_request_response(wg, VIDEO, amount), 422, "FORMAT_ERROR")

    assert error["detail"]["reason"] == "amount"
    assert len(stored_requests(wg)) == rows_before
    assert debit_requests(wg) == []
    refusals = audit_events(wg, "debit_request_refused")
    assert len(refusals) == refusals_before + 1
    row = refusals[-1]
    assert (row["actor"], row["customer_id"]) == ("system", MAIN_CUSTOMER)
    assert (row["decision"], row["reason"]) == ("FORMAT_ERROR", "amount")
    # The refusal used up nothing: the period is still free.
    assert outcome(merchant_request(wg, VIDEO, CAP)) == (AWAITING, None)
    assert_period_rules(wg)


# T100


@pytest.mark.case("T100")
def test_t100_a_second_request_while_the_first_waits_is_period_used(wg: WgClient) -> None:
    reset_s5(wg)
    first = merchant_request(wg, VIDEO, 2_500)
    second = merchant_request(wg, VIDEO, 2_500)
    third = merchant_request(wg, VIDEO, 2_000)

    assert outcome(first) == (AWAITING, None)
    assert outcome(second) == (OUT_OF_MANDATE, "PERIOD_USED")
    assert outcome(third) == (OUT_OF_MANDATE, "PERIOD_USED")
    assert len(stored_requests(wg, VIDEO)) == 3
    assert debit_request(wg, first["request_id"])["payable"] is True
    assert debit_request(wg, second["request_id"])["payable"] is False
    assert_period_rules(wg)


@pytest.mark.case("T100")
def test_t100_a_paid_period_takes_no_other_request(wg: WgClient) -> None:
    reset_s5(wg)
    first = merchant_request(wg, VIDEO, 2_500)
    pay_request(wg, first["request_id"])
    assert debit_request(wg, first["request_id"])["status"] == PAID

    later = merchant_request(wg, VIDEO, 2_500)

    assert outcome(later) == (OUT_OF_MANDATE, "PERIOD_USED")
    assert [r["status"] for r in stored_requests(wg, VIDEO)].count(PAID) == 1
    assert_period_rules(wg)


@pytest.mark.case("T100")
def test_t100_a_declined_period_takes_no_other_request(wg: WgClient) -> None:
    reset_s5(wg)
    operations = operation_count(wg)
    first = merchant_request(wg, VIDEO, 2_500)
    decline_request(wg, first["request_id"])
    assert debit_request(wg, first["request_id"])["status"] == DECLINED

    later = merchant_request(wg, VIDEO, 2_500)

    assert outcome(later) == (OUT_OF_MANDATE, "PERIOD_USED")
    assert debit_request(wg, first["request_id"])["status"] == DECLINED
    assert operation_count(wg) == operations
    assert_period_rules(wg)


@pytest.mark.case("T100")
@pytest.mark.parametrize(
    "amounts", [(3_000, 1_500), (1_500, 3_000)], ids=["big-first", "small-first"]
)
def test_t100_splitting_4500_does_not_get_two_requests_through(
    wg: WgClient, amounts: tuple[int, int]
) -> None:
    reset_s5(wg)
    first = merchant_request(wg, VIDEO, amounts[0])
    second = merchant_request(wg, VIDEO, amounts[1])

    assert outcome(first) == (AWAITING, None)
    assert outcome(second) == (OUT_OF_MANDATE, "PERIOD_USED")
    waiting = [r for r in stored_requests(wg, VIDEO) if r["status"] == AWAITING]
    assert [r["amount_fen"] for r in waiting] == [amounts[0]]
    assert_period_rules(wg)


@pytest.mark.case("T100")
def test_t100_of_two_parallel_requests_exactly_one_waits(wg: WgClient) -> None:
    reset_s5(wg)
    for ref, amounts in (
        (VIDEO, (2_000, 2_500)),
        (CLOUD, (2_100, 2_000)),
        (FITNESS, (19_900, 20_000)),
    ):
        responses = race(
            wg,
            [
                lambda tab, r=ref, a=amount: merchant_request_response(tab, r, a)
                for amount in amounts
            ],
        )
        results = [r.json() for r in responses]
        assert all(r.status_code == 200 for r in responses), [describe(r) for r in responses]
        assert Counter(outcome(r) for r in results) == Counter(
            [(AWAITING, None), (OUT_OF_MANDATE, "PERIOD_USED")]
        ), results
        assert len(stored_requests(wg, ref)) == 2
        assert [r["status"] for r in stored_requests(wg, ref)].count(AWAITING) == 1
    assert_period_rules(wg)


@pytest.mark.case("T100")
def test_t100_the_same_request_sent_in_parallel_is_stored_once(wg: WgClient) -> None:
    reset_s5(wg)
    responses = race(
        wg,
        [lambda tab: merchant_request_response(tab, VIDEO, 2_500, request_id="m-twin")] * 2,
    )
    results = [r.json() for r in responses]

    assert all(r.status_code == 200 for r in responses), [describe(r) for r in responses]
    assert results[0]["request_id"] == results[1]["request_id"]
    assert sorted(r["duplicate"] for r in results) == [False, True]
    assert all(outcome(r) == (AWAITING, None) for r in results)
    assert len(stored_requests(wg)) == 1
    assert_period_rules(wg)


@pytest.mark.case("T100")
def test_t100_requests_either_side_of_midnight_fall_into_two_periods(wg: WgClient) -> None:
    reset_s5(wg)
    wg.advance(to=at(11, 30, 23, 59))
    november = merchant_request(wg, VIDEO, 2_500, request_id="m-nov")
    wg.advance(to=at(12, 1, 0, 1))
    december = merchant_request(wg, VIDEO, 2_500, request_id="m-dec")

    assert outcome(november) == (AWAITING, None)
    assert outcome(december) == (AWAITING, None)
    assert debit_request(wg, november["request_id"])["period_key"] == "2026-11"
    assert debit_request(wg, december["request_id"])["period_key"] == "2026-12"
    # Each month still takes only one.
    assert outcome(merchant_request(wg, VIDEO, 2_500)) == (OUT_OF_MANDATE, "PERIOD_USED")
    assert_period_rules(wg)


@pytest.mark.case("T100")
def test_t100_a_repeated_merchant_request_id_returns_the_original(wg: WgClient) -> None:
    reset_s5(wg)
    original = merchant_request(wg, VIDEO, 2_500, request_id="m-once")
    assert original["duplicate"] is False

    again = merchant_request(wg, VIDEO, 2_500, request_id="m-once")
    assert again == {**original, "duplicate": True}
    assert audit_events(wg, "debit_request_repeat_mismatch") == []

    # A repeat with another amount returns the stored request and is audited.
    changed = merchant_request(wg, VIDEO, 2_900, request_id="m-once")
    assert changed == {**original, "duplicate": True}
    assert len(audit_events(wg, "debit_request_repeat_mismatch")) == 1
    stored = stored_requests(wg, VIDEO)
    assert [(r["merchant_request_id"], r["amount_fen"]) for r in stored] == [("m-once", 2_500)]

    # Also in a later month: the original stays in its own period.
    wg.advance(to=at(12, 2, 9, 0))
    late = merchant_request(wg, VIDEO, 2_500, request_id="m-once")
    assert late["request_id"] == original["request_id"] and late["duplicate"] is True
    assert debit_request(wg, original["request_id"])["period_key"] == "2026-11"
    assert len(stored_requests(wg)) == 1
    assert_period_rules(wg)


@pytest.mark.case("T100")
def test_t100_stored_rows_equal_the_distinct_merchant_request_ids(wg: WgClient) -> None:
    reset_s5(wg)
    sends = [
        ("m-a", VIDEO, 2_500),
        ("m-b", VIDEO, 2_500),
        ("m-a", VIDEO, 2_500),
        ("m-c", CLOUD, 2_100),
        ("m-b", VIDEO, 2_500),
        ("m-c", CLOUD, 2_100),
        ("m-d", CLOUD, 2_100),
        ("m-a", VIDEO, 2_500),
    ]
    results = [merchant_request(wg, ref, amount, request_id=rid) for rid, ref, amount in sends]

    stored = stored_requests(wg)
    assert len(stored) == len({rid for rid, _, _ in sends}) == 4
    assert sorted(r["merchant_request_id"] for r in stored) == ["m-a", "m-b", "m-c", "m-d"]
    assert [r["duplicate"] for r in results] == [False, False, True, False, True, True, False, True]
    first_seen: dict[str, str] = {}
    for (rid, _, _), result in zip(sends, results, strict=True):
        assert first_seen.setdefault(rid, result["request_id"]) == result["request_id"]
    assert_period_rules(wg)


@pytest.mark.case("T100")
def test_t100_a_fourth_request_in_a_period_is_period_used_or_above_the_cap(wg: WgClient) -> None:
    reset_s5(wg)
    results = [merchant_request(wg, VIDEO, amount) for amount in (2_500, 2_000, 1_500, 1_000)]
    above = merchant_request(wg, VIDEO, CAP + 1)

    assert [outcome(r) for r in results] == [
        (AWAITING, None),
        (OUT_OF_MANDATE, "PERIOD_USED"),
        (OUT_OF_MANDATE, "PERIOD_USED"),
        (OUT_OF_MANDATE, "PERIOD_USED"),
    ]
    # The cap is checked first.
    assert outcome(above) == (OUT_OF_MANDATE, "CAP")
    assert len(stored_requests(wg, VIDEO)) == 5
    assert_period_rules(wg)


@pytest.mark.case("T100")
def test_t100_three_stored_requests_close_the_period_even_if_none_waits(wg: WgClient) -> None:
    reset_s5(wg)
    # mdt_cloud caps at 2,100: all three are out of mandate and use no period.
    above = [merchant_request(wg, CLOUD, amount) for amount in (2_500, 2_400, 3_000)]
    fourth = merchant_request(wg, CLOUD, 2_100)
    fifth = merchant_request(wg, CLOUD, 2_200)

    assert [outcome(r) for r in above] == [(OUT_OF_MANDATE, "CAP")] * 3
    assert outcome(fourth) == (OUT_OF_MANDATE, "PERIOD_USED")
    assert outcome(fifth) == (OUT_OF_MANDATE, "CAP")
    assert not any(r["status"] == AWAITING for r in stored_requests(wg, CLOUD))
    assert_period_rules(wg)
