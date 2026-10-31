"""S5 direct debits: closing, suspending and expiring agreements; T101 and T102 at layers D and H.

T101 follows the guided form for "关闭健身房的自动扣款": a MANDATE_CANCEL draft, card and confirm,
then the probe that checks the effect and the protection that stays until the debit day has
passed (S5-ACCEPTANCE §3.3). T102 sends merchant requests to agreements that no longer authorize
them. A case passes on stored requests, agreement events, operations and receipts. Every S5
contract name is a string here, so the module imports on a build without S5.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from wg.contracts.enums import Fault, Role

from ..harness.client import MAIN_ACCOUNT, WgClient
from .common import active_locks, assert_cash
from .direct_debit import (
    AUTO_DECLINED,
    AWAITING,
    CLOUD,
    EPOCH,
    FITNESS,
    VIDEO,
    approve,
    assert_subscriptions,
    at,
    change_agreement,
    customer_requests,
    debit_request,
    debit_request_response,
    debit_requests,
    decision_draft_response,
    draft_change,
    effect_check,
    effect_events,
    mandate,
    mandate_events,
    mandates,
    merchant_request,
    needs_changes,
    operations_of,
    payment_operations,
    probes,
    receipt,
    refused,
    reset_s5,
    stored_mandate,
    stored_requests,
    subscription_periods,
    subscription_spend,
)

pytestmark = needs_changes


# T101


@pytest.mark.case("T101")
def test_t101_closing_the_gym_agreement_settles_and_is_verified(wg: WgClient) -> None:
    reset_s5(wg)
    before = mandate(wg, FITNESS)
    events_before = mandate_events(wg, FITNESS)
    assert [(e["field"], e["new"]) for e in events_before] == [("status", "ACTIVE")]

    draft = draft_change(wg, FITNESS, "cancel")
    assert (draft["kind"], draft["tier"], draft["feasible"]) == ("MANDATE_CANCEL", "YELLOW", True)
    done = approve(wg, draft)

    assert done["verify"] is None and done["confirm"]["challenge"] is None
    assert done["operation"] is not None and done["operation"]["status"] == "SETTLED"
    (stored,) = operations_of(wg, "MANDATE_CANCEL")
    assert stored["operation_id"] == done["operation"]["operation_id"]
    assert stored["status"] == "SETTLED" and stored["amount_fen"] is None
    assert stored["account_id"] == MAIN_ACCOUNT
    # A change moves no money and holds none.
    assert wg.db.ledger_entries(stored["operation_id"]) == []
    assert wg.db.reservations() == []

    # One immutable receipt that names the agreement and its effect check.
    receipts = wg.db.receipts(stored["operation_id"])
    assert len(receipts) == 1 and receipts[0]["status"] == "SETTLED"
    shown = receipt(wg, stored["operation_id"])
    assert (shown["kind"], shown["status"]) == ("MANDATE_CANCEL", "SETTLED")
    assert shown["receipt_id"] == receipts[0]["receipt_id"]
    assert shown["mandate_ref"] == FITNESS and shown["merchant_display"] == "模拟健身会所"
    assert shown["amount_fen"] is None and shown["request_id"] is None
    assert shown["effect_check"] == "verified"

    # The agreement: CANCELLED, one new status event that carries the operation.
    after = mandate(wg, FITNESS)
    assert after["status"] == "CANCELLED" and after["next_debit_date"] is None
    assert after["version"] == before["version"] + 1
    row = stored_mandate(wg, FITNESS)
    assert row["status"] == "CANCELLED" and datetime.fromisoformat(row["cancelled_at"]) == EPOCH
    events = mandate_events(wg, FITNESS)
    assert events[: len(events_before)] == events_before
    (event,) = events[len(events_before) :]
    assert (event["field"], event["old"], event["new"]) == ("status", "ACTIVE", "CANCELLED")
    assert event["operation_id"] == stored["operation_id"] and event["actor"] == "customer"

    # One probe at the expected amount, declined because the agreement is closed.
    (probe,) = stored_requests(wg, FITNESS)
    assert probe["probe"] == 1 and probe["amount_fen"] == 19_900
    assert (probe["status"], probe["reason_code"]) == (AUTO_DECLINED, "MANDATE_CANCELLED")
    assert probe["merchant_request_id"].startswith(f"probe-{stored['operation_id']}-")
    assert [
        (e["check_kind"], e["result"], e["probe_request_id"])
        for e in effect_events(wg, stored["operation_id"])
    ] == [("probe_cancel", "verified", probe["request_id"])]

    # The probe is in no list and no total.
    assert debit_requests(wg) == [] and debit_requests(wg, mandate_ref=FITNESS) == []
    assert debit_request_response(wg, probe["request_id"]).status_code == 404
    assert after["pending_requests"] == 0
    assert subscription_spend(wg)["total_fen"] == 0
    assert mandates(wg)["monthly_total_fen"] == 4_600
    assert sorted(
        m["mandate_ref"] for m in mandates(wg)["mandates"] if m["status"] == "ACTIVE"
    ) == (sorted([VIDEO, CLOUD]))
    wg.login(Role.OPERATOR)
    assert active_locks(wg) == []


@pytest.mark.case("T101")
def test_t101_protection_stays_until_the_debit_day_has_passed(wg: WgClient) -> None:
    reset_s5(wg)
    change_agreement(wg, FITNESS, "cancel")

    # Closing frees nothing at the click: the November period stays protected.
    assert_cash(wg, c=1_800_000, protected=1_624_500, headroom=175_500)
    assert_subscriptions(wg, 24_500)

    wg.advance(to=at(11, 20, 23, 59))
    periods = assert_subscriptions(wg, 29_100)
    assert (FITNESS, "2026-11") in periods and (FITNESS, "2026-12") not in periods

    wg.advance(to=at(11, 21, 0, 0))
    periods = assert_subscriptions(wg, 9_200)
    assert not [key for key in periods if key[0] == FITNESS]

    for instant in (at(11, 30, 23, 59), at(12, 1, 0, 0), at(12, 21, 0, 0)):
        wg.advance(to=instant)
        assert not [key for key in subscription_periods(wg) if key[0] == FITNESS], instant


@pytest.mark.case("T101")
def test_t101_a_waiting_request_is_declined_with_the_agreement(wg: WgClient) -> None:
    reset_s5(wg)
    waiting = merchant_request(wg, FITNESS, 19_900)
    assert waiting["status"] == AWAITING and mandate(wg, FITNESS)["pending_requests"] == 1

    change_agreement(wg, FITNESS, "cancel")

    view = debit_request(wg, waiting["request_id"])
    assert (view["status"], view["reason_code"]) == (AUTO_DECLINED, "MANDATE_CANCELLED")
    assert view["payable"] is False and view["operation_id"] is None
    assert mandate(wg, FITNESS)["pending_requests"] == 0
    assert payment_operations(wg) == []
    assert [r["request_id"] for r in customer_requests(wg, FITNESS)] == [waiting["request_id"]]
    refused(decision_draft_response(wg, waiting["request_id"], "pay"), 409, "REQUEST_NOT_PAYABLE")


@pytest.mark.case("T101")
def test_t101_a_timed_out_check_stays_pending_and_is_retried(wg: WgClient) -> None:
    reset_s5(wg)
    wg.arm(Fault.PROBE_TIMEOUT)

    done = change_agreement(wg, FITNESS, "cancel")

    operation_id = done["operation"]["operation_id"]
    assert done["operation"]["status"] == "SETTLED"
    assert mandate(wg, FITNESS)["status"] == "CANCELLED"
    [first] = effect_events(wg, operation_id)
    assert (first["result"], first["reason"]) == ("pending", "probe_timeout")
    assert effect_check(wg, operation_id) == "pending" and probes(wg, FITNESS) == []
    receipt_id = wg.db.receipts(operation_id)[0]["receipt_id"]
    wg.login(Role.OPERATOR)
    assert active_locks(wg) == []

    # The next sweep probes again; the receipt itself does not change.
    wg.advance(minutes=1)

    assert [e["result"] for e in effect_events(wg, operation_id)] == ["pending", "verified"]
    assert effect_check(wg, operation_id) == "verified"
    (probe,) = probes(wg, FITNESS)
    assert (probe["status"], probe["reason_code"]) == (AUTO_DECLINED, "MANDATE_CANCELLED")
    receipts = wg.db.receipts(operation_id)
    assert [r["receipt_id"] for r in receipts] == [receipt_id]
    assert receipts[0]["status"] == "SETTLED"
    assert debit_requests(wg) == []
