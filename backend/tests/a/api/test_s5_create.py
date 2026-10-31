"""Opening a new direct-debit agreement over HTTP (S5-SPEC §3.2, §5.9, §6.1, §6.3, §6.4).

The direct-debit profile is written to tmp_path and loaded into the app's database. Cases
are judged on the stored agreements and their events, actions, operations, ledger entries,
receipts, the cash snapshot, locks and audit rows.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from wg.contracts.api import Card, DraftResult, MandatesView
from wg.contracts.enums import Fault, LockScope, Role
from wg.contracts.fixture import load_fixture
from wg.k6 import fixture, locks
from wg.k6.simtime import SHANGHAI

from ..s5_profile import write_profile
from .conftest import Browser
from .s5_flows import complete, confirm, open_card

pytestmark = pytest.mark.layer("H")

EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)
EXISTING = {"mdt_video", "mdt_cloud", "mdt_fitness"}
START_PROTECTED = 1_624_500
START_HEADROOM = 175_500


@pytest.fixture
def s5(browser: Browser, tmp_path: Path) -> Browser:
    fixture.reset(browser.db, load_fixture(write_profile(tmp_path)))
    browser.login("opr_demo")
    browser.login("cus_main")
    return browser


def rows(browser: Browser, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with browser.db.read() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def create_response(browser: Browser, merchant_id: str = "mer_music", **fields: Any):
    body = {
        "merchant_id": merchant_id,
        "expected_fen": 1_500,
        "cap_fen": 2_000,
        "debit_day": 15,
        "months": 12,
        **fields,
    }
    return browser.post("/v1/drafts/mandate", body)


def draft_create(browser: Browser, merchant_id: str = "mer_music", **fields: Any) -> DraftResult:
    response = create_response(browser, merchant_id, **fields)
    assert response.status_code == 201, response.text
    return DraftResult.model_validate(response.json())


def refused(response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, body
    return body


def stored(browser: Browser) -> dict[str, int]:
    """Counts of mandates, actions and operations: what a refusal must leave unchanged."""
    return {
        table: len(rows(browser, f"SELECT 1 FROM {table}"))
        for table in ("mandates", "actions", "operations")
    }


def money(browser: Browser) -> tuple[Any, ...]:
    return (
        rows(browser, "SELECT account_id, balance_fen FROM accounts ORDER BY account_id"),
        rows(browser, "SELECT * FROM ledger_entries ORDER BY rowid"),
        rows(browser, "SELECT * FROM reservations ORDER BY rowid"),
    )


def cash(browser: Browser) -> dict[str, Any]:
    response = browser.get("/v1/cash/snapshot")
    assert response.status_code == 200, response.text
    return response.json()


def created(browser: Browser) -> list[dict[str, Any]]:
    return rows(
        browser,
        "SELECT * FROM mandates WHERE mandate_ref NOT IN ('mdt_video', 'mdt_cloud',"
        " 'mdt_fitness', 'mdt_other_video')",
    )


# Confirming


@pytest.mark.case("T107")
def test_a_registry_merchant_gets_a_red_card_and_an_active_agreement(s5: Browser):
    before = money(s5)
    result = draft_create(s5)
    assert (result.kind.value, result.tier.value, result.feasible) == (
        "MANDATE_CREATE",
        "RED",
        True,
    )

    card = open_card(s5, result)
    lines = {line.key: line.text for line in card.lines}
    assert card.title == "新增自动扣款：模拟音乐会员"
    assert (lines["expected"], lines["cap"], lines["debit_day"]) == ("¥15.00", "¥20.00", "每月15日")
    assert (lines["expires_on"], lines["next_debit_date"]) == ("2027-11-01", "2026-11-15")
    assert "未在回复截止前处理的请求不会扣款" in card.warnings
    assert "确认后需要输入短信验证码。" in card.warnings
    protection = {c.key: (c.before, c.after) for c in card.changes}
    assert protection == {
        "protected": (START_PROTECTED, START_PROTECTED + 1_500),
        "headroom": (START_HEADROOM, START_HEADROOM - 1_500),
    }
    assert rows(s5, "SELECT 1 FROM mandates WHERE merchant_id = 'mer_music'") == []

    done = complete(s5, card)

    assert card.requires_code and done["operation"]["status"] == "SETTLED"
    (row,) = created(s5)
    assert (row["merchant_id"], row["customer_id"], row["account_id"]) == (
        "mer_music",
        "cus_main",
        "acc_main",
    )
    assert (row["status"], row["version"], row["cancelled_at"]) == ("ACTIVE", 1, None)
    assert (row["expected_fen"], row["cap_fen"], row["debit_day"]) == (1_500, 2_000, 15)
    assert (row["starts_on"], row["expires_on"]) == ("2026-11-02", "2027-11-01")
    assert row["mandate_ref"].startswith("mdt_") and row["purpose_id"].startswith("pur_")
    (operation,) = rows(s5, "SELECT * FROM operations WHERE kind = 'MANDATE_CREATE'")
    assert operation["amount_fen"] is None and operation["account_id"] == "acc_main"
    (event,) = rows(s5, "SELECT * FROM mandate_events WHERE mandate_ref = ?", (row["mandate_ref"],))
    assert (event["field"], event["old"], event["new"]) == ("status", None, "ACTIVE")
    assert (event["operation_id"], event["actor"]) == (operation["operation_id"], "customer")
    assert money(s5) == before


@pytest.mark.case("T107")
def test_the_first_period_is_protected_from_the_moment_of_confirming(s5: Browser):
    card = open_card(s5, draft_create(s5))
    complete(s5, card)

    snapshot = cash(s5)
    assert snapshot["protected_fen"] == START_PROTECTED + 1_500
    assert snapshot["headroom_fen"] == START_HEADROOM - 1_500
    view = MandatesView.model_validate(s5.get("/v1/mandates").json())
    (agreement,) = [m for m in view.mandates if m.mandate_ref not in EXISTING | {"mdt_other_video"}]
    assert view.monthly_total_fen == 24_500 + 1_500
    assert agreement.next_debit_date == date(2026, 11, 15)
    assert [(p.period_key, p.due_date, p.expected_fen) for p in agreement.protection] == [
        ("2026-11", date(2026, 11, 15), 1_500)
    ]
    assert agreement.merchant_display == "模拟音乐会员"
    periods = {
        (row["mandate_ref"], row["period_key"]): row["expected_fen"]
        for row in snapshot["details"]["subscriptions"]
    }
    assert periods[(agreement.mandate_ref, "2026-11")] == 1_500


def test_the_new_agreement_receives_and_holds_merchant_requests(s5: Browser):
    complete(s5, open_card(s5, draft_create(s5)))
    (row,) = created(s5)

    sent = s5.sim(
        "/sim/debit-requests",
        {
            "merchant_id": "mer_music",
            "merchant_request_id": "music-1",
            "mandate_ref": row["mandate_ref"],
            "amount_fen": 1_500,
            "currency": "CNY",
        },
    )

    assert sent.status_code == 200 and sent.json()["status"] == "AWAITING_CLIENT", sent.text


def test_a_short_term_ends_the_day_before_the_same_day_of_a_later_month(s5: Browser):
    complete(s5, open_card(s5, draft_create(s5, months=1, debit_day=31)))

    (row,) = created(s5)
    assert (row["starts_on"], row["expires_on"], row["debit_day"]) == (
        "2026-11-02",
        "2026-12-01",
        31,
    )
    view = MandatesView.model_validate(s5.get("/v1/mandates").json())
    (agreement,) = [m for m in view.mandates if m.mandate_ref == row["mandate_ref"]]
    assert agreement.next_debit_date == date(2026, 11, 30)


def test_a_customer_gets_an_agreement_on_their_own_main_account(s5: Browser):
    s5.login("cus_other")

    complete(s5, open_card(s5, draft_create(s5)))

    (row,) = created(s5)
    assert (row["customer_id"], row["account_id"]) == ("cus_other", "acc_other")


def test_a_lost_acknowledgement_stores_the_agreement_once_until_reconciled(s5: Browser):
    card = open_card(s5, draft_create(s5))
    assert s5.sim("/sim/faults", {"fault": Fault.BANK_ACK_LOST.value}).status_code == 200

    done = complete(s5, card)

    operation = done["operation"]
    assert operation["status"] == "UNKNOWN"
    assert len(created(s5)) == 1
    assert (
        rows(s5, "SELECT * FROM receipts WHERE operation_id = ?", (operation["operation_id"],))
        == []
    )

    s5.login("opr_ops")
    reconciled = s5.post("/v1/operator/reconcile", {"customer_id": "cus_main"}, role=Role.OPERATOR)

    assert reconciled.status_code == 200, reconciled.text
    (settled,) = rows(s5, "SELECT * FROM operations WHERE kind = 'MANDATE_CREATE'")
    assert settled["status"] == "SETTLED"
    assert (
        len(rows(s5, "SELECT * FROM receipts WHERE operation_id = ?", (settled["operation_id"],)))
        == 1
    )
    assert len(created(s5)) == 1
    orders = rows(
        s5,
        "SELECT * FROM bank_requests WHERE idempotency_key = ?",
        (settled["idempotency_key"],),
    )
    assert len(orders) == 1


def test_a_confirmed_card_cannot_create_a_second_agreement(s5: Browser):
    card = open_card(s5, draft_create(s5))
    complete(s5, card)

    again = confirm(s5, Card.model_validate(s5.get(f"/v1/cards/{card.card_id}").json()))

    assert again.status_code in (200, 409), again.text
    assert len(created(s5)) == 1
    assert len(rows(s5, "SELECT * FROM operations WHERE kind = 'MANDATE_CREATE'")) == 1


# Refusals


@pytest.mark.case("T107")
def test_an_unknown_merchant_is_not_found_and_nothing_is_stored(s5: Browser):
    before = stored(s5)

    refused(create_response(s5, "mer_nowhere"), 404, "NOT_FOUND")

    assert stored(s5) == before


@pytest.mark.case("T107")
def test_an_unverified_merchant_is_refused_with_its_reason(s5: Browser):
    before = stored(s5)

    error = refused(create_response(s5, "mer_unverified"), 422, "UNSUPPORTED_REQUEST")

    assert error["detail"]["reason"] == "merchant_unverified"
    assert stored(s5) == before
    audit = rows(s5, "SELECT * FROM audit_log WHERE event = 'draft_refused'")
    assert [(a["ref"], a["decision"]) for a in audit] == [("mer_unverified", "UNSUPPORTED_REQUEST")]


@pytest.mark.case("T107")
@pytest.mark.parametrize("months", [13, 24])
def test_a_term_over_twelve_months_is_refused_with_its_reason(s5: Browser, months: int):
    before = stored(s5)

    error = refused(create_response(s5, months=months), 422, "UNSUPPORTED_REQUEST")

    assert error["detail"]["reason"] == "term_over_12_months"
    assert stored(s5) == before


@pytest.mark.parametrize(
    "fields", [{"months": 0}, {"debit_day": 0}, {"debit_day": 32}, {"cap_fen": 1_499}]
)
def test_a_malformed_request_is_refused_by_its_contract(s5: Browser, fields: dict[str, Any]):
    before = stored(s5)

    refused(create_response(s5, **fields), 422, "UNSUPPORTED_REQUEST")

    assert stored(s5) == before


def test_a_cap_above_the_single_limit_is_refused(s5: Browser):
    before = stored(s5)

    refused(create_response(s5, cap_fen=5_000_001), 422, "SINGLE_LIMIT_EXCEEDED")

    assert stored(s5) == before


def test_a_second_draft_for_the_same_merchant_is_a_duplicate(s5: Browser):
    first = draft_create(s5)

    error = refused(create_response(s5, cap_fen=2_500), 409, "DUPLICATE_INTENT")

    assert error["detail"]["logical_action_id"] == first.logical_action_id
    assert len(rows(s5, "SELECT 1 FROM actions WHERE kind = 'MANDATE_CREATE'")) == 1


@pytest.mark.case("T107")
def test_a_draft_alone_creates_no_agreement_and_no_protection(s5: Browser):
    operations = stored(s5)["operations"]
    draft_create(s5)

    assert created(s5) == []
    assert cash(s5)["protected_fen"] == START_PROTECTED
    assert stored(s5)["operations"] == operations


def test_a_merchant_unverified_after_the_card_refuses_the_code(s5: Browser):
    card = open_card(s5, draft_create(s5))
    challenge = confirm(s5, card).json()["challenge"]["challenge_id"]
    (code,) = [
        m["code"] for m in s5.get("/v1/sms").json()["messages"] if m["challenge_id"] == challenge
    ]
    with s5.db.unit_of_work("test registry") as uow:
        uow.execute("UPDATE merchants SET verified = 0 WHERE merchant_id = 'mer_music'")
    operations = stored(s5)["operations"]

    response = s5.post(f"/v1/challenges/{challenge}/verify", {"code": code})

    error = refused(response, 422, "UNSUPPORTED_REQUEST")
    assert error["detail"]["reason"] == "merchant_unverified"
    assert created(s5) == []
    assert stored(s5)["operations"] == operations


# Locks (T104)


@pytest.mark.case("T104")
def test_under_a_customer_lock_a_new_agreement_is_refused_at_every_step(s5: Browser):
    open_one = open_card(s5, draft_create(s5))
    with s5.db.unit_of_work("test lock") as uow:
        locks.set_lock(uow, LockScope.CUSTOMER, "cus_main", "test")
    before = (stored(s5), money(s5))

    refused(confirm(s5, open_one), 423, "LOCKED_WRITE")
    refused(create_response(s5, "mer_cloud"), 423, "LOCKED_WRITE")

    assert (stored(s5), money(s5)) == before
    assert created(s5) == []
