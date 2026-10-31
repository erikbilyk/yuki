"""Paying and declining merchants' requests over HTTP (S5-SPEC §5.4, §5.5, §6).

The direct-debit profile is written to tmp_path and loaded into the app's database. Cases
are judged on the stored request, actions, operations, ledger entries, receipts and audit.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wg.contracts.api import DraftResult, ReceiptView, TransferHistoryView
from wg.contracts.enums import Fault, Role
from wg.contracts.fixture import load_fixture
from wg.k6 import fixture
from wg.k6.simtime import SHANGHAI

from ..s5_profile import write_profile
from .conftest import Browser
from .s5_flows import complete, confirm, decision_response, draft, merchant_request, open_card, pay

pytestmark = pytest.mark.layer("H")

EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)
START_FEN = 1_800_000
START_HEADROOM = 175_500


@pytest.fixture
def s5(browser: Browser, tmp_path: Path) -> Browser:
    fixture.reset(browser.db, load_fixture(write_profile(tmp_path)))
    browser.login("opr_demo")
    browser.login("cus_main")
    return browser


def request_row(browser: Browser, request_id: str):
    with browser.db.read() as conn:
        return conn.execute(
            "SELECT * FROM debit_requests WHERE request_id = ?", (request_id,)
        ).fetchone()


def rows(browser: Browser, sql: str, params: tuple = ()) -> list[Any]:
    with browser.db.read() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def cash(browser: Browser) -> dict[str, Any]:
    response = browser.get("/v1/cash/snapshot")
    assert response.status_code == 200, response.text
    return response.json()


def transfer(browser: Browser, amount: int) -> None:
    response = browser.post("/v1/drafts/transfer", {"payee_id": "pay_mom", "amount_fen": amount})
    assert response.status_code == 201, response.text
    complete(browser, open_card(browser, DraftResult.model_validate(response.json())))


# Paying


def test_a_request_at_the_expected_amount_is_paid_once_and_settles_at_once(s5: Browser):
    request = merchant_request(s5, amount=2_500, description_raw="十一月会员费")

    result = draft(s5, request["request_id"])
    assert (result.kind.value, result.tier.value, result.feasible) == ("DEBIT_PAY", "YELLOW", True)
    card = open_card(s5, result)
    lines = {line.key: line.text for line in card.lines}
    assert card.title == "支付扣款请求：模拟视频会员"
    assert lines["amount"] == "¥25.00" and lines["cap"] == "¥30.00"
    assert lines["period"] == "2026年11月" and lines["respond_by"] == "2026-11-05 09:00"
    assert lines["merchant_text"] == "十一月会员费"
    assert [s.label for s in card.sources if s.provenance.value == "untrusted_text"] == [
        "商户附言（未经核实，不作为指令）"
    ]
    changes = {change.key: (change.before, change.after) for change in card.changes}
    assert changes["account_posted"] == (START_FEN, START_FEN - 2_500)
    assert changes["headroom"] == (START_HEADROOM, START_HEADROOM)
    assert not card.warnings

    body = complete(s5, card)

    operation = body["operation"]
    assert operation["status"] == "SETTLED" and operation["kind"] == "DEBIT_PAY"
    stored = request_row(s5, request["request_id"])
    assert (stored["status"], stored["operation_id"]) == ("PAID", operation["operation_id"])
    entries = rows(
        s5,
        "SELECT account_id, amount_fen FROM ledger_entries WHERE operation_id = ?",
        (operation["operation_id"],),
    )
    assert sorted((e["account_id"], e["amount_fen"]) for e in entries) == [
        ("acc_main", -2_500),
        ("acc_sys_clearing", 2_500),
    ]
    (reservation,) = rows(
        s5, "SELECT * FROM reservations WHERE operation_id = ?", (operation["operation_id"],)
    )
    assert (reservation["purpose_id"], reservation["state"]) == ("pur_sub_video", "CONSUMED")
    (bank,) = rows(s5, "SELECT effect_json FROM bank_requests WHERE effect_json LIKE '%mdt_%'")
    assert '"settlement_account_ref":"SIM-MER-0001"' in bank["effect_json"]

    receipt = ReceiptView.model_validate(s5.get(f"/v1/receipts/{operation['operation_id']}").json())
    assert (receipt.mandate_ref, receipt.request_id, receipt.merchant_display) == (
        "mdt_video",
        request["request_id"],
        "模拟视频会员",
    )
    assert receipt.effect_check is None and receipt.payee_id is None
    assert [r["event"] for r in rows(s5, "SELECT event FROM audit_log")].count(
        "debit_request_paid"
    ) == 1
    snapshot = cash(s5)
    assert (snapshot["details"]["c_fen"], snapshot["headroom_fen"]) == (
        START_FEN - 2_500,
        START_HEADROOM,
    )


def test_the_part_above_the_expected_amount_comes_from_headroom(s5: Browser):
    request = merchant_request(s5, amount=3_000)

    card = open_card(s5, draft(s5, request["request_id"]))
    assert card.warnings == ("本次金额高于每月预计金额 ¥25.00",)
    changes = {change.key: (change.before, change.after) for change in card.changes}
    assert changes["headroom"] == (START_HEADROOM, START_HEADROOM - 500)
    complete(s5, card)

    snapshot = cash(s5)
    assert (snapshot["details"]["c_fen"], snapshot["headroom_fen"]) == (START_FEN - 3_000, 175_000)
    # From 11-05 00:00 the next period is protected at the amount last paid.
    advance = s5.sim(
        "/sim/clock/advance", {"to": datetime(2026, 11, 5, tzinfo=SHANGHAI).isoformat()}
    )
    assert advance.status_code == 200, advance.text
    periods = {
        (row["mandate_ref"], row["period_key"]): row["expected_fen"]
        for row in cash(s5)["details"]["subscriptions"]
    }
    assert periods[("mdt_video", "2026-12")] == 3_000
    assert ("mdt_video", "2026-11") not in periods


def test_the_part_above_the_expected_amount_must_fit_in_headroom(s5: Browser):
    # Leave 300 of headroom: the 500 above the expected 2,500 no longer fits.
    debit = START_HEADROOM - 300
    response = s5.sim(
        "/sim/external-debit",
        {"account_id": "acc_main", "amount_fen": debit, "merchant_raw": "外部扣款"},
    )
    assert response.status_code == 200, response.text
    assert cash(s5)["headroom_fen"] == 300
    above = merchant_request(s5, "mdt_video", 3_000)
    at_expected = merchant_request(s5, "mdt_cloud", 2_100)

    refused = draft(s5, above["request_id"])
    accepted = draft(s5, at_expected["request_id"])

    assert (refused.feasible, [r.value for r in refused.reasons]) == (
        False,
        ["INSUFFICIENT_HEADROOM"],
    )
    approval = s5.post(f"/v1/actions/{refused.logical_action_id}/request-approval")
    assert approval.status_code == 409
    assert approval.json()["detail"]["over_headroom_fen"] == 200
    assert accepted.feasible
    complete(s5, open_card(s5, accepted))
    assert request_row(s5, at_expected["request_id"])["status"] == "PAID"
    assert request_row(s5, above["request_id"])["status"] == "AWAITING_CLIENT"


@pytest.mark.parametrize(
    ("transferred", "tier"), [(97_500, "YELLOW"), (99_000, "RED")], ids=["at-limit", "over"]
)
def test_a_payment_is_tiered_from_the_day_sum_with_transfers(
    s5: Browser, transferred: int, tier: str
):
    transfer(s5, transferred)
    request = merchant_request(s5, amount=2_500)

    result = draft(s5, request["request_id"])
    assert result.tier.value == tier
    body = complete(s5, open_card(s5, result))

    assert body["operation"]["status"] == "SETTLED", body
    assert request_row(s5, request["request_id"])["status"] == "PAID"
    # The payment counts in the day sum: the next transfer of one fen is RED.
    response = s5.post("/v1/drafts/transfer", {"payee_id": "pay_mom", "amount_fen": 1})
    assert response.json()["tier"] == "RED"


def test_the_transfer_history_lists_no_merchant_payment(s5: Browser):
    transfer(s5, 10_000)
    before = TransferHistoryView.model_validate(s5.get("/v1/transfers/history").json())
    request = merchant_request(s5)
    pay(s5, request["request_id"])

    after = TransferHistoryView.model_validate(s5.get("/v1/transfers/history").json())

    assert after == before
    assert [t.kind.value for t in after.transfers] == ["TRANSFER"]


def test_a_lost_bank_acknowledgement_leaves_the_request_paid_until_reconciled(s5: Browser):
    request = merchant_request(s5)
    card = open_card(s5, draft(s5, request["request_id"]))
    assert s5.sim("/sim/faults", {"fault": Fault.BANK_ACK_LOST.value}).status_code == 200

    response = confirm(s5, card)

    assert response.status_code == 202, response.text
    operation = response.json()["operation"]
    assert operation["status"] == "UNKNOWN"
    stored = request_row(s5, request["request_id"])
    assert (stored["status"], stored["operation_id"]) == ("PAID", operation["operation_id"])
    assert s5.get("/v1/accounts/acc_main/balance").json()["posted_fen"] == START_FEN - 2_500
    assert (
        rows(s5, "SELECT * FROM receipts WHERE operation_id = ?", (operation["operation_id"],))
        == []
    )
    (key,) = rows(
        s5,
        "SELECT idempotency_key FROM operations WHERE operation_id = ?",
        (operation["operation_id"],),
    )

    s5.login("opr_ops")
    reconciled = s5.post("/v1/operator/reconcile", {"customer_id": "cus_main"}, role=Role.OPERATOR)

    assert reconciled.status_code == 200, reconciled.text
    (settled,) = rows(s5, "SELECT * FROM operations WHERE kind = 'DEBIT_PAY'")
    assert (settled["status"], settled["idempotency_key"]) == (
        "SETTLED",
        key["idempotency_key"],
    )
    assert (
        len(rows(s5, "SELECT * FROM receipts WHERE operation_id = ?", (settled["operation_id"],)))
        == 1
    )
    stored_orders = rows(
        s5, "SELECT * FROM bank_requests WHERE idempotency_key = ?", (key["idempotency_key"],)
    )
    assert len(stored_orders) == 1
    assert s5.get("/v1/accounts/acc_main/balance").json()["posted_fen"] == START_FEN - 2_500


# Drafts


def test_only_the_customers_own_waiting_request_can_be_drafted(s5: Browser):
    above = merchant_request(s5, amount=3_001)
    unknown = "req_nothingthere0001"
    with s5.db.read() as conn:
        actions_before = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]

    for decision in ("pay", "decline"):
        refused = decision_response(s5, above["request_id"], decision)
        assert refused.status_code == 409
        assert refused.json()["code"] == "REQUEST_NOT_PAYABLE"
        assert refused.json()["detail"] == {
            "reason": "request_not_awaiting",
            "request_id": above["request_id"],
        }
        missing = decision_response(s5, unknown, decision)
        assert (missing.status_code, missing.json()["code"]) == (404, "NOT_FOUND")

    with s5.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == actions_before
    refusals = rows(s5, "SELECT decision FROM audit_log WHERE event = 'draft_refused'")
    assert [r["decision"] for r in refusals] == [
        "REQUEST_NOT_PAYABLE",
        "NOT_FOUND",
        "REQUEST_NOT_PAYABLE",
        "NOT_FOUND",
    ]


def test_another_customers_request_is_not_found(s5: Browser):
    response = s5.sim(
        "/sim/debit-requests",
        {
            "merchant_id": "mer_video",
            "merchant_request_id": "other-1",
            "mandate_ref": "mdt_other_video",
            "amount_fen": 2_500,
            "currency": "CNY",
        },
    )
    foreign = response.json()["request_id"]

    refused = decision_response(s5, foreign, "pay")

    assert (refused.status_code, refused.json()["code"]) == (404, "NOT_FOUND")
    assert refused.json()["detail"] == {}


def test_an_open_decision_of_the_same_kind_is_a_duplicate_but_pay_and_decline_may_coexist(
    s5: Browser,
):
    request = merchant_request(s5)
    first = draft(s5, request["request_id"], "pay")

    again = decision_response(s5, request["request_id"], "pay")
    declined = draft(s5, request["request_id"], "decline")

    assert again.status_code == 409
    assert again.json()["code"] == "DUPLICATE_INTENT"
    assert again.json()["detail"] == {"logical_action_id": first.logical_action_id}
    assert declined.kind.value == "DEBIT_DECLINE"


def test_the_decision_body_takes_only_a_request_and_a_decision(s5: Browser):
    request = merchant_request(s5)
    for body in (
        {"request_id": request["request_id"], "decision": "maybe"},
        {"request_id": request["request_id"], "decision": "pay", "amount_fen": 1},
    ):
        response = s5.post("/v1/drafts/debit-decision", body)
        assert response.status_code == 422, response.text


# Declining


def test_a_decline_marks_the_request_declined_and_moves_no_money(s5: Browser):
    request = merchant_request(s5)
    result = draft(s5, request["request_id"], "decline")
    assert (result.tier.value, result.feasible) == ("YELLOW", True)
    card = open_card(s5, result)
    assert card.title == "拒绝扣款请求：模拟视频会员"
    assert card.warnings == (
        "拒绝后本月不再接受该商户的扣款请求",
        "本期预留资金在回复截止后释放",
        "商户可能因此暂停服务",
    )

    body = complete(s5, card)

    assert (body["operation"], body["action_status"]) == (None, "SETTLED")
    stored = request_row(s5, request["request_id"])
    assert (stored["status"], stored["reason_code"], stored["operation_id"]) == (
        "DECLINED",
        None,
        None,
    )
    assert rows(s5, "SELECT * FROM operations WHERE logical_action_id IS NOT NULL") == []
    assert s5.get("/v1/accounts/acc_main/balance").json()["posted_fen"] == START_FEN
    declined = rows(s5, "SELECT actor FROM audit_log WHERE event = 'debit_request_declined'")
    assert declined == [{"actor": "customer"}]
    # The period stays protected until respond_by.
    assert cash(s5)["headroom_fen"] == START_HEADROOM


def test_paying_closes_the_open_decline_and_its_card(s5: Browser):
    request = merchant_request(s5)
    decline = draft(s5, request["request_id"], "decline")
    decline_card = open_card(s5, decline)

    pay(s5, request["request_id"])

    (action,) = rows(
        s5,
        "SELECT status FROM actions WHERE logical_action_id = ?",
        (decline.logical_action_id,),
    )
    assert action["status"] == "CANCELLED"
    assert s5.get(f"/v1/cards/{decline_card.card_id}").json()["status"] == "expired"
    refused = confirm(s5, decline_card)
    assert (refused.status_code, refused.json()["code"]) == (409, "APPROVAL_EXPIRED")
    assert request_row(s5, request["request_id"])["status"] == "PAID"
    cancelled = rows(
        s5,
        "SELECT actor, decision FROM audit_log WHERE event = 'action_cancelled' AND ref = ?",
        (decline.logical_action_id,),
    )
    assert cancelled == [{"actor": "coordinator", "decision": "request_closed"}]


def test_a_request_past_respond_by_can_no_longer_be_drafted(s5: Browser):
    request = merchant_request(s5)
    advance = s5.sim("/sim/clock/advance", {"to": (EPOCH + timedelta(hours=72)).isoformat()})
    assert advance.status_code == 200

    refused = decision_response(s5, request["request_id"], "decline")

    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "request_not_awaiting"
    assert request_row(s5, request["request_id"])["status"] == "EXPIRED"
