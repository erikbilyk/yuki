"""S5 direct debits: raising a cap with requests waiting above it; R-S5-1 at layer D.

Requests above the cap are stored as OUT_OF_MANDATE CAP. When the customer raises the cap with a
confirmed RED card, each period promotes its earliest eligible request, the others become
PERIOD_USED, and nothing is paid (S5-SPEC §5.3, S5-ACCEPTANCE §3.3). A case passes on stored
requests, operations and the ledger. Every S5 contract name is a string here, so the module
imports on a build without S5.

Agreement mdt_cloud (expected and cap 2,100) takes the requests.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ..harness.client import WgClient
from .common import assert_cash
from .direct_debit import (
    AWAITING,
    CLOUD,
    OUT_OF_MANDATE,
    PAID,
    assert_period_rules,
    at,
    audit_events,
    change_agreement,
    debit_request,
    has_direct_debits,
    merchant_request,
    money_state,
    outcome,
    pay_request,
    payment_operations,
    reset_s5,
    stored_requests,
)

pytestmark = pytest.mark.skipif(
    not has_direct_debits(), reason="the build lists no direct-debit routes"
)


def request_above_cap(wg: WgClient, amount_fen: int) -> dict:
    """A cloud request that exceeds the cap; it is stored as CAP and uses no period."""
    result = merchant_request(wg, CLOUD, amount_fen)
    assert outcome(result) == (OUT_OF_MANDATE, "CAP"), result
    return result


def stored_row(wg: WgClient, request_id: str) -> dict:
    (row,) = [r for r in stored_requests(wg) if r["request_id"] == request_id]
    return row


def raise_cap(wg: WgClient, cap_fen: int) -> dict:
    done = change_agreement(wg, CLOUD, "set_cap", cap_fen)
    assert done["draft"]["kind"] == "MANDATE_CAP_RAISE" and done["draft"]["tier"] == "RED"
    assert done["verify"] is not None and done["card"]["requires_code"] is True
    return done


@pytest.mark.case("R-S5-1")
@pytest.mark.layer("D")
def test_rs51_the_earliest_eligible_request_is_promoted_and_nothing_is_paid(
    app_client: WgClient,
) -> None:
    wg = app_client
    reset_s5(wg)
    first = request_above_cap(wg, 2_500)
    wg.advance(minutes=1)
    second = request_above_cap(wg, 2_400)
    wg.advance(minutes=1)
    third = request_above_cap(wg, 3_000)
    respond_by = debit_request(wg, first["request_id"])["respond_by"]
    state = money_state(wg)

    raise_cap(wg, 2_500)

    promoted = debit_request(wg, first["request_id"])
    assert promoted["status"] == AWAITING and promoted["reason_code"] is None
    assert promoted["payable"] is True and promoted["operation_id"] is None
    assert datetime.fromisoformat(promoted["respond_by"]) == datetime.fromisoformat(respond_by)
    # Only one request of a period is answerable; the other eligible one is closed with it.
    other = debit_request(wg, second["request_id"])
    assert (other["status"], other["reason_code"]) == (OUT_OF_MANDATE, "PERIOD_USED")
    still_above = debit_request(wg, third["request_id"])
    assert (still_above["status"], still_above["reason_code"]) == (OUT_OF_MANDATE, "CAP")
    assert [r["status"] for r in stored_requests(wg, CLOUD) if not r["probe"]].count(AWAITING) == 1
    # Two rows changed, each with an audit row; no payment and no money moved.
    updated = audit_events(wg, "debit_request_updated")
    assert sorted(row["decision"] for row in updated) == [AWAITING, OUT_OF_MANDATE]
    assert payment_operations(wg) == []
    after = money_state(wg)
    assert (after["posted"], after["ledger"], after["reservations"]) == (
        state["posted"],
        state["ledger"],
        state["reservations"],
    )
    assert_period_rules(wg)

    # Paying it later draws the 400 above the expected 2,100 from headroom.
    paid = pay_request(wg, first["request_id"])
    assert paid["operation"]["status"] == "SETTLED"
    assert debit_request(wg, first["request_id"])["status"] == PAID
    assert_cash(wg, c=1_797_500, protected=1_622_400, headroom=175_100)
    assert len(payment_operations(wg)) == 1


@pytest.mark.case("R-S5-1")
@pytest.mark.layer("D")
def test_rs51_a_period_with_a_paid_request_promotes_nothing(app_client: WgClient) -> None:
    wg = app_client
    reset_s5(wg)
    first = request_above_cap(wg, 2_500)
    second = request_above_cap(wg, 2_400)
    # A request within the cap is the third stored one, still accepted, and the customer pays it.
    within = merchant_request(wg, CLOUD, 2_100)
    assert outcome(within) == (AWAITING, None)
    pay_request(wg, within["request_id"])
    assert debit_request(wg, within["request_id"])["status"] == PAID
    state = money_state(wg)

    raise_cap(wg, 2_500)

    for request in (first, second):
        view = debit_request(wg, request["request_id"])
        assert (view["status"], view["reason_code"]) == (OUT_OF_MANDATE, "PERIOD_USED")
        assert view["payable"] is False and view["operation_id"] is None
    assert debit_request(wg, within["request_id"])["status"] == PAID
    assert len(payment_operations(wg)) == 1 == len(state["payments"])
    assert money_state(wg)["posted"] == state["posted"]
    assert money_state(wg)["ledger"] == state["ledger"]
    assert [r["status"] for r in stored_requests(wg, CLOUD) if not r["probe"]].count(PAID) == 1
    assert_period_rules(wg)


@pytest.mark.case("R-S5-1")
@pytest.mark.layer("D")
def test_rs51_a_request_past_its_reply_time_is_left_alone(app_client: WgClient) -> None:
    wg = app_client
    reset_s5(wg)
    late = request_above_cap(wg, 2_500)
    wg.advance(to=at(11, 5, 9, 1))
    late_view = debit_request(wg, late["request_id"])
    assert datetime.fromisoformat(late_view["respond_by"]) == at(11, 5, 9, 0)
    fresh = request_above_cap(wg, 2_400)
    late_before = stored_row(wg, late["request_id"])

    raise_cap(wg, 2_500)

    # The late request is untouched; the fresh one fills the free period.
    assert stored_row(wg, late["request_id"]) == late_before
    view = debit_request(wg, fresh["request_id"])
    assert view["status"] == AWAITING and view["payable"] is True
    assert debit_request(wg, late["request_id"])["status"] == OUT_OF_MANDATE
    assert payment_operations(wg) == []
    assert_period_rules(wg)


@pytest.mark.case("R-S5-1")
@pytest.mark.layer("D")
def test_rs51_a_paid_period_and_a_late_request_together(app_client: WgClient) -> None:
    wg = app_client
    reset_s5(wg)
    late = request_above_cap(wg, 2_500)
    wg.advance(to=at(11, 5, 9, 1))
    eligible = request_above_cap(wg, 2_400)
    within = merchant_request(wg, CLOUD, 2_100)
    assert outcome(within) == (AWAITING, None)
    pay_request(wg, within["request_id"])
    late_before = stored_row(wg, late["request_id"])

    raise_cap(wg, 2_500)

    view = debit_request(wg, eligible["request_id"])
    assert (view["status"], view["reason_code"]) == (OUT_OF_MANDATE, "PERIOD_USED")
    assert stored_row(wg, late["request_id"]) == late_before
    assert len(payment_operations(wg)) == 1
    assert_period_rules(wg)


@pytest.mark.case("R-S5-1")
@pytest.mark.layer("D")
def test_rs51_each_period_promotes_its_own_earliest_request(app_client: WgClient) -> None:
    wg = app_client
    reset_s5(wg)
    wg.advance(to=at(11, 30, 23, 59))
    november = request_above_cap(wg, 2_500)
    wg.advance(to=at(12, 1, 0, 1))
    december = request_above_cap(wg, 2_500)
    assert debit_request(wg, november["request_id"])["period_key"] == "2026-11"
    assert debit_request(wg, december["request_id"])["period_key"] == "2026-12"

    raise_cap(wg, 2_500)

    for request in (november, december):
        view = debit_request(wg, request["request_id"])
        assert view["status"] == AWAITING and view["payable"] is True
    assert payment_operations(wg) == []
    assert_period_rules(wg)
