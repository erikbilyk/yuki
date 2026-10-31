"""Direct-debit routes over HTTP: merchants' requests through the simulator and the reads.

The direct-debit profile is written to tmp_path and loaded into the app's database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wg.contracts.api import (
    DebitRequestsView,
    DebitRequestView,
    MandatesView,
    MerchantsView,
    SimDebitResult,
    SubscriptionSpendView,
)
from wg.contracts.enums import OperationStatus, Role
from wg.contracts.fixture import load_fixture
from wg.k6 import fixture, operations
from wg.k6.debit_requests import IncomingDebit, receive_in
from wg.k6.simtime import SHANGHAI

from ..s5_profile import write_profile
from .conftest import Browser

pytestmark = pytest.mark.layer("H")

EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)
MERCHANT_OF = {
    "mdt_video": "mer_video",
    "mdt_cloud": "mer_cloud",
    "mdt_fitness": "mer_fitness",
    "mdt_other_video": "mer_video",
}
_serial = iter(range(1, 10_000))


@pytest.fixture
def s5(browser: Browser, tmp_path: Path) -> Browser:
    """The browser on the direct-debit profile, signed in again as the panel and cus_main."""
    fixture.reset(browser.db, load_fixture(write_profile(tmp_path)))
    browser.login("opr_demo")
    browser.login("cus_main")
    return browser


def merchant_request(
    browser: Browser,
    ref: str = "mdt_video",
    amount: Any = 2_500,
    *,
    rid: str | None = None,
    merchant: str | None = None,
    currency: str = "CNY",
    description: str = "",
):
    return browser.sim(
        "/sim/debit-requests",
        {
            "merchant_id": merchant or MERCHANT_OF.get(ref, "mer_video"),
            "merchant_request_id": rid or f"m-{next(_serial)}",
            "mandate_ref": ref,
            "amount_fen": amount,
            "currency": currency,
            "description_raw": description,
        },
    )


def accepted(browser: Browser, ref: str = "mdt_video", amount: int = 2_500, **options):
    response = merchant_request(browser, ref, amount, **options)
    assert response.status_code == 200, response.text
    return SimDebitResult.model_validate(response.json())


def ok(response, model):
    assert response.status_code == 200, response.text
    return model.model_validate(response.json())


def advance(browser: Browser, to: datetime) -> None:
    assert browser.sim("/sim/clock/advance", {"to": to.isoformat()}).status_code == 200


def set_request(browser: Browser, request_id: str, **columns: Any) -> None:
    sets = ", ".join(f"{name} = ?" for name in columns)
    with browser.db.unit_of_work("test request") as uow:
        uow.execute(
            f"UPDATE debit_requests SET {sets} WHERE request_id = ?",
            (*columns.values(), request_id),
        )


# POST /sim/debit-requests


def test_a_merchant_request_is_received_and_a_repeat_returns_it(s5: Browser):
    first = accepted(s5, amount=2_500, rid="m-once", description="会员费")
    assert (first.status.value, first.reason_code, first.duplicate) == (
        "AWAITING_CLIENT",
        None,
        False,
    )
    assert first.respond_by == EPOCH + timedelta(hours=72)

    again = merchant_request(s5, amount=2_500, rid="m-once")
    assert again.status_code == 200
    assert again.json() == {**first.model_dump(mode="json"), "duplicate": True}
    above = accepted(s5, amount=3_001)
    assert (above.status.value, above.reason_code.value if above.reason_code else None) == (
        "OUT_OF_MANDATE",
        "CAP",
    )


@pytest.mark.parametrize(
    ("amount", "currency", "reason"),
    [(0, "CNY", "amount"), (-500, "CNY", "amount"), (2_500, "USD", "currency")],
    ids=["zero", "negative", "currency"],
)
def test_a_malformed_request_is_422_format_error_and_not_stored(
    s5: Browser, amount: int, currency: str, reason: str
):
    response = merchant_request(s5, amount=amount, currency=currency)

    assert response.status_code == 422
    body = response.json()
    assert (body["code"], body["detail"]) == ("FORMAT_ERROR", {"reason": reason})
    assert body["message_zh"] == "扣款请求格式不正确，未被受理"
    with s5.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM debit_requests").fetchone()[0] == 0
        refusals = conn.execute(
            "SELECT customer_id, decision, reason FROM audit_log"
            " WHERE event = 'debit_request_refused'"
        ).fetchall()
    assert [tuple(row) for row in refusals] == [("cus_main", "FORMAT_ERROR", reason)]


@pytest.mark.parametrize("amount", ["2500", 25.0, None], ids=["string", "float", "null"])
def test_a_non_integer_amount_fails_body_validation(s5: Browser, amount: Any):
    response = merchant_request(s5, amount=amount)
    assert response.status_code == 422
    assert response.json()["code"] == "UNSUPPORTED_REQUEST"


def test_the_simulator_route_needs_the_demo_role_and_its_csrf_token(s5: Browser):
    body = {
        "merchant_id": "mer_video",
        "merchant_request_id": "m-x",
        "mandate_ref": "mdt_video",
        "amount_fen": 2_500,
        "currency": "CNY",
    }
    assert s5.post("/sim/debit-requests", body, role=Role.CUSTOMER).status_code == 403
    assert s5.post("/sim/debit-requests", body, role=Role.DEMO, csrf=False).status_code == 403
    with s5.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM debit_requests").fetchone()[0] == 0


# GET /v1/mandates and /v1/merchants


def test_the_agreements_at_the_epoch(s5: Browser):
    view = ok(s5.get("/v1/mandates"), MandatesView)

    assert view.as_of == EPOCH
    assert view.monthly_total_fen == 24_500
    assert [
        (m.mandate_ref, m.merchant_display, m.status.value, m.expected_fen, m.cap_fen)
        + (m.debit_day, m.next_debit_date, m.pending_requests, m.version)
        for m in view.mandates
    ] == [
        ("mdt_cloud", "模拟云盘", "ACTIVE", 2_100, 2_100, 12, date(2026, 11, 12), 0, 1),
        ("mdt_fitness", "模拟健身会所", "ACTIVE", 19_900, 22_000, 20, date(2026, 11, 20), 0, 1),
        ("mdt_video", "模拟视频会员", "ACTIVE", 2_500, 3_000, 5, date(2026, 11, 5), 0, 1),
    ]
    video = view.mandates[2]
    assert (video.account_id, video.starts_on, video.expires_on) == (
        "acc_main",
        date(2026, 10, 2),
        date(2027, 10, 1),
    )
    assert [(p.period_key, p.due_date, p.expected_fen, p.until) for p in video.protection] == [
        ("2026-11", date(2026, 11, 5), 2_500, None)
    ]


def test_agreement_views_follow_requests_and_status(s5: Browser):
    accepted(s5, "mdt_video", 2_500)
    accepted(s5, "mdt_video", 2_000)
    with s5.db.unit_of_work("test suspend") as uow:
        uow.execute(
            "INSERT INTO mandate_events (mandate_ref, field, old, new, actor, reason, at)"
            " VALUES ('mdt_cloud', 'status', 'ACTIVE', 'SUSPENDED', 'customer', 'test', 't')"
        )
        uow.execute("UPDATE mandates SET status = 'SUSPENDED' WHERE mandate_ref = 'mdt_cloud'")

    view = ok(s5.get("/v1/mandates"), MandatesView)
    by_ref = {m.mandate_ref: m for m in view.mandates}
    assert by_ref["mdt_video"].pending_requests == 1
    assert by_ref["mdt_cloud"].status.value == "SUSPENDED"
    assert by_ref["mdt_cloud"].next_debit_date == date(2026, 11, 12)
    # The monthly total counts ACTIVE agreements only.
    assert view.monthly_total_fen == 22_400


def test_the_other_customer_sees_only_their_agreement(s5: Browser):
    s5.login("cus_other")
    view = ok(s5.get("/v1/mandates"), MandatesView)
    assert [m.mandate_ref for m in view.mandates] == ["mdt_other_video"]
    assert view.monthly_total_fen == 2_500


def test_merchants_lists_only_verified_ones(s5: Browser):
    view = ok(s5.get("/v1/merchants"), MerchantsView)
    assert [(m.merchant_id, m.display_name) for m in view.merchants] == [
        ("mer_cloud", "模拟云盘"),
        ("mer_fitness", "模拟健身会所"),
        ("mer_music", "模拟音乐会员"),
        ("mer_video", "模拟视频会员"),
    ]


# GET /v1/debit-requests and /v1/debit-requests/{id}


def add_probe(browser: Browser, ref: str = "mdt_video", amount: int = 2_500) -> str:
    with browser.db.unit_of_work("test probe") as uow:
        received = receive_in(
            uow,
            IncomingDebit(MERCHANT_OF[ref], f"probe-opn_x-{next(_serial)}", ref, amount, "CNY"),
            probe=True,
        )
    return received.request.request_id


def test_requests_list_only_own_non_probe_requests_newest_first(s5: Browser):
    first = accepted(s5, "mdt_video", 2_500, description="十一月会员费")
    second = accepted(s5, "mdt_cloud", 2_500)
    accepted(s5, "mdt_other_video", 2_500)
    accepted(s5, "mdt_video", 2_500, merchant="mer_cloud")  # names no agreement of mer_cloud
    add_probe(s5)

    view = ok(s5.get("/v1/debit-requests"), DebitRequestsView)

    assert [r.request_id for r in view.requests] == [second.request_id, first.request_id]
    newest, oldest = view.requests
    assert (newest.status.value, newest.reason_code.value if newest.reason_code else None) == (
        "OUT_OF_MANDATE",
        "CAP",
    )
    assert (oldest.mandate_ref, oldest.merchant_id, oldest.merchant_display) == (
        "mdt_video",
        "mer_video",
        "模拟视频会员",
    )
    assert (oldest.amount_fen, oldest.currency, oldest.period_key) == (2_500, "CNY", "2026-11")
    assert oldest.description_raw == "十一月会员费"
    assert (oldest.payable, newest.payable) == (True, False)
    assert oldest.respond_by == EPOCH + timedelta(hours=72)


def test_requests_can_be_filtered_by_status_and_agreement(s5: Browser):
    video = accepted(s5, "mdt_video", 2_500)
    accepted(s5, "mdt_cloud", 2_500)

    awaiting = ok(s5.get("/v1/debit-requests", status="AWAITING_CLIENT"), DebitRequestsView)
    assert [r.request_id for r in awaiting.requests] == [video.request_id]
    by_ref = ok(s5.get("/v1/debit-requests", mandate_ref="mdt_cloud"), DebitRequestsView)
    assert [r.mandate_ref for r in by_ref.requests] == ["mdt_cloud"]
    foreign = ok(s5.get("/v1/debit-requests", mandate_ref="mdt_other_video"), DebitRequestsView)
    assert foreign.requests == ()
    assert s5.get("/v1/debit-requests", status="WAITING").status_code == 422


def test_a_foreign_probe_or_unknown_request_is_the_same_404(s5: Browser):
    own = accepted(s5, "mdt_video", 2_500)
    foreign = accepted(s5, "mdt_other_video", 2_500)
    probe = add_probe(s5, "mdt_cloud", 2_100)
    stray = accepted(s5, "mdt_video", 2_500, merchant="mer_unverified")

    view = ok(s5.get(f"/v1/debit-requests/{own.request_id}"), DebitRequestView)
    assert view.request_id == own.request_id and view.payable is True

    unknown = s5.get("/v1/debit-requests/req_0000000000000000")
    assert unknown.status_code == 404
    for request_id in (foreign.request_id, probe, stray.request_id):
        response = s5.get(f"/v1/debit-requests/{request_id}")
        assert (response.status_code, response.json()) == (404, unknown.json()), request_id


def test_payable_follows_deadline_agreement_and_cap(s5: Browser):
    request = accepted(s5, "mdt_video", 2_500)

    def payable() -> bool:
        return ok(s5.get(f"/v1/debit-requests/{request.request_id}"), DebitRequestView).payable

    assert payable() is True
    with s5.db.unit_of_work("test cap") as uow:
        uow.execute(
            "INSERT INTO mandate_events (mandate_ref, field, old, new, actor, reason, at)"
            " VALUES ('mdt_video', 'cap_fen', '3000', '2000', 'customer', 'test', 't')"
        )
        uow.execute("UPDATE mandates SET cap_fen = 2000 WHERE mandate_ref = 'mdt_video'")
    assert payable() is False
    with s5.db.unit_of_work("test cap") as uow:
        uow.execute(
            "INSERT INTO mandate_events (mandate_ref, field, old, new, actor, reason, at)"
            " VALUES ('mdt_video', 'cap_fen', '2000', '3000', 'customer', 'test', 't')"
        )
        uow.execute("UPDATE mandates SET cap_fen = 3000 WHERE mandate_ref = 'mdt_video'")
    assert payable() is True
    advance(s5, EPOCH + timedelta(hours=72))
    assert payable() is False


# GET /v1/subscriptions/spend


def settled_operation(browser: Browser) -> str:
    """A settled same-bank transfer of cus_main, to stand in for a request's payment."""
    draft = browser.post("/v1/drafts/transfer", {"payee_id": "pay_mom", "amount_fen": 10_000})
    assert draft.status_code == 201, draft.text
    action_id = draft.json()["logical_action_id"]
    approval = browser.post(f"/v1/actions/{action_id}/request-approval")
    card = browser.get(f"/v1/cards/{approval.json()['card_id']}").json()
    confirm = browser.post(
        f"/v1/cards/{card['card_id']}/confirm", {"terms_hash": card["terms_hash"]}
    )
    assert confirm.status_code in (200, 201), confirm.text
    return confirm.json()["operation"]["operation_id"]


def test_spend_lists_every_agreement_and_counts_paid_requests_only(s5: Browser):
    paid = accepted(s5, "mdt_video", 2_800)
    accepted(s5, "mdt_cloud", 2_100)
    set_request(s5, paid.request_id, status="PAID", operation_id=settled_operation(s5))

    view = ok(s5.get("/v1/subscriptions/spend"), SubscriptionSpendView)

    assert view.days == 90 and view.total_fen == 2_800
    rows = {row.mandate_ref: row for row in view.rows}
    assert list(rows) == ["mdt_cloud", "mdt_fitness", "mdt_video"]
    video = rows["mdt_video"]
    assert (video.merchant_display, video.paid_count, video.paid_fen) == ("模拟视频会员", 1, 2_800)
    assert (video.last_paid_on, video.last_amount_fen) == (date(2026, 11, 2), 2_800)
    assert (rows["mdt_cloud"].paid_count, rows["mdt_cloud"].last_paid_on) == (0, None)
    # From now on the paid amount is the agreement's expected amount.
    mandates = ok(s5.get("/v1/mandates"), MandatesView)
    assert {m.mandate_ref: m.expected_fen for m in mandates.mandates}["mdt_video"] == 2_800


@pytest.mark.parametrize("days", [0, 91])
def test_spend_days_must_be_within_1_and_90(s5: Browser, days: int):
    assert s5.get("/v1/subscriptions/spend", days=days).status_code == 422


def test_spend_window_leaves_out_older_payments(s5: Browser):
    paid = accepted(s5, "mdt_video", 2_500)
    set_request(s5, paid.request_id, status="PAID", operation_id=settled_operation(s5))
    advance(s5, EPOCH + timedelta(days=31))

    assert ok(s5.get("/v1/subscriptions/spend", days=30), SubscriptionSpendView).total_fen == 0
    assert ok(s5.get("/v1/subscriptions/spend", days=31), SubscriptionSpendView).total_fen == 2_500


# GET /v1/cash/snapshot


def test_the_snapshot_route_lists_subscriptions_apart(s5: Browser):
    response = s5.get("/v1/cash/snapshot")
    assert response.status_code == 200
    body = response.json()
    assert (body["headroom_fen"], body["unpaid_bills_fen"], body["unpaid_bills_count"]) == (
        175_500,
        840_000,
        2,
    )
    assert [
        (s["mandate_ref"], s["period_key"], s["status"]) for s in body["details"]["subscriptions"]
    ] == [
        ("mdt_video", "2026-11", "secured"),
        ("mdt_cloud", "2026-11", "secured"),
        ("mdt_fitness", "2026-11", "secured"),
    ]
    bills = s5.get("/v1/bills").json()["bills"]
    assert {b["commitment_id"] for b in bills} == {"cmt_rent", "cmt_insurance"}


# GET /v1/transfers/history (S5-SPEC §5.9)


def test_the_transfer_history_lists_transfers_and_bill_payments_only(s5: Browser):
    before = s5.get("/v1/transfers/history")
    with s5.db.unit_of_work("test merchant payment") as uow:
        operations.insert_operation(
            uow,
            key="test-debit-pay",
            request_hash="h",
            kind="DEBIT_PAY",
            submitted_at=EPOCH,
            status=OperationStatus.SETTLED,
            customer_id="cus_main",
            account_id="acc_main",
            amount_fen=2_500,
            settled_at=EPOCH,
        )

    after = s5.get("/v1/transfers/history")

    assert (before.status_code, after.status_code) == (200, 200)
    assert after.json() == before.json()
