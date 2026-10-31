"""Changes of direct-debit agreements over HTTP (S5-SPEC §5.3, §5.7, §5.8, §5.10, §6).

The direct-debit profile is written to tmp_path and loaded into the app's database. Cases
are judged on the stored agreement, its events, requests, actions, operations, ledger
entries, receipts, effect checks, locks and audit rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wg.contracts.api import DraftResult, MandatesView, ReceiptView
from wg.contracts.enums import ActionKind, DebitRequestStatus, Fault, LockScope, Role
from wg.contracts.fixture import load_fixture
from wg.core import mandate_rules
from wg.k6 import fixture, locks
from wg.k6.simtime import SHANGHAI

from ..s5_profile import write_profile
from .conftest import Browser
from .s5_flows import complete, confirm, draft, merchant_request, open_card, pay

pytestmark = pytest.mark.layer("H")

EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)


def reset(browser: Browser, tmp_path: Path, variant: str | None = None) -> Browser:
    fixture.reset(browser.db, load_fixture(write_profile(tmp_path), variant), variant)
    browser.login("opr_demo")
    browser.login("cus_main")
    return browser


@pytest.fixture
def s5(browser: Browser, tmp_path: Path) -> Browser:
    return reset(browser, tmp_path)


def rows(browser: Browser, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with browser.db.read() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def change_response(browser: Browser, ref: str, change: str, cap_fen: int | None = None):
    body: dict[str, Any] = {"mandate_ref": ref, "change": change}
    if cap_fen is not None:
        body["cap_fen"] = cap_fen
    return browser.post("/v1/drafts/mandate-change", body)


def draft_change(browser: Browser, ref: str, change: str, cap_fen: int | None = None):
    response = change_response(browser, ref, change, cap_fen)
    assert response.status_code == 201, response.text
    return DraftResult.model_validate(response.json())


def change(browser: Browser, ref: str, kind: str, cap_fen: int | None = None) -> dict[str, Any]:
    return complete(browser, open_card(browser, draft_change(browser, ref, kind, cap_fen)))


def refused(response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, body
    return body


def mandate(browser: Browser, ref: str) -> dict[str, Any]:
    (row,) = rows(browser, "SELECT * FROM mandates WHERE mandate_ref = ?", (ref,))
    return row


def mandate_events(browser: Browser, ref: str) -> list[dict[str, Any]]:
    return rows(
        browser, "SELECT * FROM mandate_events WHERE mandate_ref = ? ORDER BY event_id", (ref,)
    )


def request_row(browser: Browser, request_id: str) -> dict[str, Any]:
    (row,) = rows(browser, "SELECT * FROM debit_requests WHERE request_id = ?", (request_id,))
    return row


def probes(browser: Browser, ref: str) -> list[dict[str, Any]]:
    return rows(
        browser,
        "SELECT * FROM debit_requests WHERE mandate_ref = ? AND probe = 1 ORDER BY rowid",
        (ref,),
    )


def effect_events(browser: Browser, operation_id: str) -> list[dict[str, Any]]:
    return rows(
        browser,
        "SELECT * FROM effect_verification_events WHERE operation_id = ? ORDER BY event_id",
        (operation_id,),
    )


def receipt(browser: Browser, operation_id: str) -> ReceiptView:
    response = browser.get(f"/v1/receipts/{operation_id}")
    assert response.status_code == 200, response.text
    return ReceiptView.model_validate(response.json())


def operations_of(browser: Browser, kind: str) -> list[dict[str, Any]]:
    return rows(browser, "SELECT * FROM operations WHERE kind = ? ORDER BY rowid", (kind,))


def active_locks(browser: Browser) -> list[dict[str, Any]]:
    return rows(browser, "SELECT scope, scope_ref, reason FROM locks WHERE cleared_at IS NULL")


def money(browser: Browser) -> tuple[Any, ...]:
    """Posted balances, ledger entries and reservations: what a change must never move."""
    return (
        rows(browser, "SELECT account_id, balance_fen FROM accounts ORDER BY account_id"),
        rows(browser, "SELECT * FROM ledger_entries ORDER BY rowid"),
        rows(browser, "SELECT * FROM reservations ORDER BY rowid"),
    )


def card_status(browser: Browser, card) -> str:
    response = browser.get(f"/v1/cards/{card.card_id}")
    assert response.status_code == 200, response.text
    return response.json()["status"]


# Cancel, suspend, resume


@pytest.mark.case("T101")
def test_a_cancel_settles_once_declines_waiting_requests_and_is_verified(s5: Browser):
    waiting = merchant_request(s5, "mdt_fitness", 19_900)
    before = money(s5)

    result = draft_change(s5, "mdt_fitness", "cancel")
    assert (result.kind, result.tier.value, result.feasible) == (
        ActionKind.MANDATE_CANCEL,
        "YELLOW",
        True,
    )
    card = open_card(s5, result)
    lines = {line.key: line.text for line in card.lines}
    assert card.title == "关闭自动扣款：模拟健身会所"
    assert lines["expected"] == "¥199.00" and lines["cap"] == "¥220.00"
    assert lines["next_debit_date"] == "2026-11-20" and lines["expires_on"] == "2027-10-01"
    assert "本期预留的 ¥199.00 保留到 2026-11-20，之后释放" in card.warnings
    assert "等待中的扣款请求将被自动拒绝" in card.warnings
    done = complete(s5, card)

    assert done["challenge"] is None and done["operation"]["status"] == "SETTLED"
    (operation,) = operations_of(s5, "MANDATE_CANCEL")
    assert operation["amount_fen"] is None and operation["account_id"] == "acc_main"
    assert money(s5) == before
    row = mandate(s5, "mdt_fitness")
    assert (row["status"], row["version"]) == ("CANCELLED", 2)
    assert datetime.fromisoformat(row["cancelled_at"]) == EPOCH
    *_, event = mandate_events(s5, "mdt_fitness")
    assert (event["field"], event["old"], event["new"]) == ("status", "ACTIVE", "CANCELLED")
    assert (event["operation_id"], event["actor"]) == (operation["operation_id"], "customer")
    assert len(mandate_events(s5, "mdt_fitness")) == 2
    declined = request_row(s5, waiting["request_id"])
    assert (declined["status"], declined["reason_code"]) == ("AUTO_DECLINED", "MANDATE_CANCELLED")
    updated = rows(s5, "SELECT * FROM audit_log WHERE event = 'debit_request_updated'")
    assert [(r["ref"], r["decision"]) for r in updated] == [
        (waiting["request_id"], "AUTO_DECLINED")
    ]

    # The probe ran after the confirm, at the expected amount, and was refused.
    (probe,) = probes(s5, "mdt_fitness")
    assert (probe["amount_fen"], probe["status"], probe["reason_code"]) == (
        19_900,
        "AUTO_DECLINED",
        "MANDATE_CANCELLED",
    )
    assert probe["merchant_request_id"] == f"probe-{operation['operation_id']}-1"
    assert [
        (e["check_kind"], e["result"], e["probe_request_id"])
        for e in effect_events(s5, operation["operation_id"])
    ] == [("probe_cancel", "verified", probe["request_id"])]
    shown = receipt(s5, operation["operation_id"])
    assert (shown.kind, shown.status.value) == (ActionKind.MANDATE_CANCEL, "SETTLED")
    assert (shown.mandate_ref, shown.merchant_display) == ("mdt_fitness", "模拟健身会所")
    assert shown.effect_check is not None and shown.effect_check.value == "verified"
    assert shown.amount_fen is None and shown.request_id is None
    # The probe is in no list.
    listed = s5.get("/v1/debit-requests").json()["requests"]
    assert [r["request_id"] for r in listed] == [waiting["request_id"]]
    assert s5.get(f"/v1/debit-requests/{probe['request_id']}").status_code == 404
    assert active_locks(s5) == []


def test_suspend_then_resume_with_a_code(s5: Browser):
    waiting = merchant_request(s5, "mdt_video", 2_500)

    suspended = change(s5, "mdt_video", "suspend")
    assert suspended["operation"]["status"] == "SETTLED"
    assert mandate(s5, "mdt_video")["status"] == "SUSPENDED"
    row = request_row(s5, waiting["request_id"])
    assert (row["status"], row["reason_code"]) == ("AUTO_DECLINED", "MANDATE_SUSPENDED")
    (probe,) = probes(s5, "mdt_video")
    assert (probe["status"], probe["reason_code"]) == ("AUTO_DECLINED", "MANDATE_SUSPENDED")
    assert receipt(s5, suspended["operation"]["operation_id"]).effect_check.value == "verified"

    result = draft_change(s5, "mdt_video", "resume")
    assert (result.kind, result.tier.value) == (ActionKind.MANDATE_RESUME, "RED")
    card = open_card(s5, result)
    assert card.title == "恢复自动扣款：模拟视频会员"
    assert "恢复后商户可以再次发起扣款请求，每一笔仍需您确认" in card.warnings
    first = confirm(s5, card)
    assert first.status_code in (200, 201, 202) and first.json()["challenge"] is not None
    assert mandate(s5, "mdt_video")["status"] == "SUSPENDED"
    resumed = complete(s5, card)
    assert resumed["operation"]["status"] == "SETTLED"
    assert mandate(s5, "mdt_video")["status"] == "ACTIVE"
    assert mandate(s5, "mdt_video")["version"] == 3
    # A resume is not probed.
    assert receipt(s5, resumed["operation"]["operation_id"]).effect_check is None
    assert len(probes(s5, "mdt_video")) == 1
    assert merchant_request(s5, "mdt_video", 2_500)["status"] == "AWAITING_CLIENT"


def test_change_drafts_are_refused_when_the_agreement_does_not_allow_them(s5: Browser):
    refused(change_response(s5, "mdt_video", "resume"), 409, "MANDATE_CHANGED")
    refused(change_response(s5, "mdt_video", "set_cap", 3_000), 422, "UNSUPPORTED_REQUEST")
    refused(change_response(s5, "mdt_video", "set_cap", 5_000_001), 422, "SINGLE_LIMIT_EXCEEDED")
    refused(change_response(s5, "mdt_other_video", "cancel"), 404, "NOT_FOUND")
    refused(change_response(s5, "mdt_nothing", "cancel"), 404, "NOT_FOUND")
    first = draft_change(s5, "mdt_video", "cancel")
    body = refused(change_response(s5, "mdt_video", "cancel"), 409, "DUPLICATE_INTENT")
    assert body["detail"]["logical_action_id"] == first.logical_action_id
    assert draft_change(s5, "mdt_video", "set_cap", 2_000).kind is ActionKind.MANDATE_CAP_LOWER
    assert draft_change(s5, "mdt_video", "set_cap", 3_500).kind is ActionKind.MANDATE_CAP_RAISE

    change(s5, "mdt_cloud", "cancel")
    detail = refused(change_response(s5, "mdt_cloud", "suspend"), 409, "MANDATE_CHANGED")["detail"]
    assert (detail["reason"], detail["status"]) == ("status", "CANCELLED")
    refusals = rows(s5, "SELECT decision FROM audit_log WHERE event = 'draft_refused'")
    assert len(refusals) == 7


def test_a_change_makes_the_open_change_cards_of_the_agreement_expire(s5: Browser):
    cancel_card = open_card(s5, draft_change(s5, "mdt_video", "cancel"))
    lower = draft_change(s5, "mdt_video", "set_cap", 2_000)

    change(s5, "mdt_video", "suspend")

    assert card_status(s5, cancel_card) == "expired"
    refused(confirm(s5, cancel_card), 409, "APPROVAL_EXPIRED")
    (cancelled,) = rows(
        s5, "SELECT status FROM actions WHERE logical_action_id = ?", (lower.logical_action_id,)
    )
    assert cancelled["status"] == "CANCELLED"
    reasons = rows(s5, "SELECT decision FROM audit_log WHERE event = 'action_cancelled'")
    assert [r["decision"] for r in reasons] == ["mandate_changed", "mandate_changed"]
    # A new cancel can be drafted at the new version.
    assert draft_change(s5, "mdt_video", "cancel").kind is ActionKind.MANDATE_CANCEL
    assert operations_of(s5, "MANDATE_CANCEL") == []


# Caps


@pytest.mark.case("R-S5-1")
def test_r_s5_1_a_cap_raise_promotes_the_earliest_request_and_pays_nothing(s5: Browser):
    sent = []
    for minute, amount in enumerate((2_500, 2_400, 3_000)):
        if minute:
            s5.sim("/sim/clock/advance", {"to": (EPOCH + timedelta(minutes=minute)).isoformat()})
        result = merchant_request(s5, "mdt_cloud", amount)
        assert (result["status"], result["reason_code"]) == ("OUT_OF_MANDATE", "CAP")
        sent.append(result["request_id"])
    respond_by = request_row(s5, sent[0])["respond_by"]
    before = money(s5)

    result = draft_change(s5, "mdt_cloud", "set_cap", 2_500)
    assert (result.kind, result.tier.value) == (ActionKind.MANDATE_CAP_RAISE, "RED")
    card = open_card(s5, result)
    assert [(c.key, c.before, c.after) for c in card.changes] == [("cap", 2_100, 2_500)]
    done = complete(s5, card)

    assert done["operation"]["status"] == "SETTLED"
    promoted, other, above = (request_row(s5, request_id) for request_id in sent)
    assert (promoted["status"], promoted["reason_code"]) == ("AWAITING_CLIENT", None)
    assert promoted["respond_by"] == respond_by
    assert (other["status"], other["reason_code"]) == ("OUT_OF_MANDATE", "PERIOD_USED")
    assert (above["status"], above["reason_code"]) == ("OUT_OF_MANDATE", "CAP")
    assert money(s5) == before and operations_of(s5, "DEBIT_PAY") == []
    assert receipt(s5, done["operation"]["operation_id"]).effect_check is None
    assert probes(s5, "mdt_cloud") == []
    *_, event = mandate_events(s5, "mdt_cloud")
    assert (event["field"], event["old"], event["new"]) == ("cap_fen", "2100", "2500")

    # Paying the promoted request later takes the 400 above the expected 2,100 from headroom.
    headroom = s5.get("/v1/cash/snapshot").json()["headroom_fen"]
    paid = pay(s5, sent[0])
    assert paid["operation"]["status"] == "SETTLED"
    assert s5.get("/v1/cash/snapshot").json()["headroom_fen"] == headroom - 400


@pytest.mark.case("R-S5-1")
def test_r_s5_1_a_paid_period_promotes_nothing(s5: Browser):
    above = [merchant_request(s5, "mdt_cloud", amount)["request_id"] for amount in (2_500, 2_400)]
    within = merchant_request(s5, "mdt_cloud", 2_100)
    pay(s5, within["request_id"])

    change(s5, "mdt_cloud", "set_cap", 2_500)

    for request_id in above:
        row = request_row(s5, request_id)
        assert (row["status"], row["reason_code"]) == ("OUT_OF_MANDATE", "PERIOD_USED")
    assert len(operations_of(s5, "DEBIT_PAY")) == 1


@pytest.mark.case("R-S5-1")
def test_r_s5_1_a_request_past_its_reply_time_is_untouched(s5: Browser):
    late = merchant_request(s5, "mdt_cloud", 2_500)["request_id"]
    s5.sim("/sim/clock/advance", {"to": datetime(2026, 11, 5, 9, 1, tzinfo=SHANGHAI).isoformat()})
    fresh = merchant_request(s5, "mdt_cloud", 2_400)["request_id"]
    late_before = request_row(s5, late)

    change(s5, "mdt_cloud", "set_cap", 2_500)

    assert request_row(s5, late) == late_before
    assert request_row(s5, fresh)["status"] == "AWAITING_CLIENT"


@pytest.mark.case("T103")
def test_a_lower_cap_turns_the_open_payment_card_stale_and_is_probed(s5: Browser):
    request = merchant_request(s5, "mdt_video", 2_500)
    pay_action = draft(s5, request["request_id"])
    pay_card = open_card(s5, pay_action)

    result = draft_change(s5, "mdt_video", "set_cap", 2_000)
    assert (result.kind, result.tier.value) == (ActionKind.MANDATE_CAP_LOWER, "YELLOW")
    lowered = complete(s5, open_card(s5, result))

    row = request_row(s5, request["request_id"])
    assert (row["status"], row["reason_code"]) == ("OUT_OF_MANDATE", "CAP")
    assert card_status(s5, pay_card) == "expired"
    (action,) = rows(
        s5,
        "SELECT status FROM actions WHERE logical_action_id = ?",
        (pay_action.logical_action_id,),
    )
    assert action["status"] == "CANCELLED"
    refused(confirm(s5, pay_card), 409, "APPROVAL_EXPIRED")
    assert operations_of(s5, "DEBIT_PAY") == []
    assert request_row(s5, request["request_id"])["status"] == "OUT_OF_MANDATE"
    (probe,) = probes(s5, "mdt_video")
    assert (probe["amount_fen"], probe["status"], probe["reason_code"]) == (
        2_001,
        "OUT_OF_MANDATE",
        "CAP",
    )
    operation_id = lowered["operation"]["operation_id"]
    assert [e["check_kind"] for e in effect_events(s5, operation_id)] == ["probe_cap"]
    assert receipt(s5, operation_id).effect_check.value == "verified"


# Probes (S5-SPEC §5.7)


def test_a_timed_out_probe_stays_pending_and_the_next_sweep_retries_it(s5: Browser):
    assert s5.sim("/sim/faults", {"fault": Fault.PROBE_TIMEOUT.value}).status_code == 200

    done = change(s5, "mdt_fitness", "cancel")

    operation_id = done["operation"]["operation_id"]
    [first] = effect_events(s5, operation_id)
    assert (first["check_kind"], first["result"], first["reason"]) == (
        "probe_cancel",
        "pending",
        "probe_timeout",
    )
    assert probes(s5, "mdt_fitness") == []
    assert receipt(s5, operation_id).effect_check.value == "pending"
    assert active_locks(s5) == []

    s5.sim("/sim/clock/advance", {"to": (EPOCH + timedelta(minutes=1)).isoformat()})

    assert [e["result"] for e in effect_events(s5, operation_id)] == ["pending", "verified"]
    (probe,) = probes(s5, "mdt_fitness")
    assert probe["merchant_request_id"] == f"probe-{operation_id}-2"
    assert receipt(s5, operation_id).effect_check.value == "verified"
    assert len(rows(s5, "SELECT * FROM receipts WHERE operation_id = ?", (operation_id,))) == 1


def test_an_accepted_probe_fails_the_check_and_locks_the_customer(
    s5: Browser, monkeypatch: pytest.MonkeyPatch
):
    decide = mandate_rules.decide_request

    def accept_probes(*args: Any, **kwargs: Any):
        if kwargs.get("probe"):
            return DebitRequestStatus.AWAITING_CLIENT, None
        return decide(*args, **kwargs)

    monkeypatch.setattr(mandate_rules, "decide_request", accept_probes)

    done = change(s5, "mdt_fitness", "suspend")

    operation_id = done["operation"]["operation_id"]
    assert done["operation"]["status"] == "SETTLED"
    (probe,) = probes(s5, "mdt_fitness")
    assert probe["status"] == "AWAITING_CLIENT"
    assert [(e["result"], e["probe_request_id"]) for e in effect_events(s5, operation_id)] == [
        ("failed", probe["request_id"])
    ]
    shown = receipt(s5, operation_id)
    assert shown.status.value == "SETTLED" and shown.effect_check.value == "failed"
    assert active_locks(s5) == [
        {
            "scope": LockScope.CUSTOMER.value,
            "scope_ref": "cus_main",
            "reason": "INTEGRITY_VIOLATION: probe_accepted",
        }
    ]
    (failure,) = rows(s5, "SELECT * FROM failure_records")
    assert (failure["error_class"], "probe_accepted" in failure["detail"]) == ("integrity", True)
    assert rows(s5, "SELECT event FROM audit_log WHERE event = 'effect_check_failed'")
    # A failed check is final: later sweeps do not probe again.
    s5.sim("/sim/clock/advance", {"to": (EPOCH + timedelta(minutes=1)).isoformat()})
    assert len(effect_events(s5, operation_id)) == 1
    assert s5.get("/v1/mandates").status_code == 200


# Expiry (S5-SPEC §5.8 step 2)


@pytest.mark.case("T102")
def test_an_agreement_expires_at_its_instant_during_one_clock_jump(
    browser: Browser, tmp_path: Path
):
    s5 = reset(browser, tmp_path, "video_expiring")
    assert mandate(s5, "mdt_video")["expires_on"] == "2026-11-05"
    s5.sim("/sim/clock/advance", {"to": datetime(2026, 11, 5, 23, 0, tzinfo=SHANGHAI).isoformat()})
    waiting = merchant_request(s5, "mdt_video", 2_500)
    assert waiting["status"] == "AWAITING_CLIENT"

    response = s5.sim(
        "/sim/clock/advance", {"to": datetime(2026, 11, 10, 9, 0, tzinfo=SHANGHAI).isoformat()}
    )
    assert response.status_code == 200, response.text

    row = mandate(s5, "mdt_video")
    assert (row["status"], row["expires_on"], row["version"]) == ("EXPIRED", "2026-11-05", 2)
    *_, event = mandate_events(s5, "mdt_video")
    assert (event["field"], event["old"], event["new"]) == ("status", "ACTIVE", "EXPIRED")
    assert (event["actor"], event["reason"], event["operation_id"]) == (
        "coordinator",
        "expires_on",
        None,
    )
    assert datetime.fromisoformat(event["at"]) == datetime(2026, 11, 6, 0, 0, tzinfo=SHANGHAI)
    declined = request_row(s5, waiting["request_id"])
    assert (declined["status"], declined["reason_code"]) == ("AUTO_DECLINED", "MANDATE_EXPIRED")
    (audit,) = rows(s5, "SELECT * FROM audit_log WHERE event = 'mandate_expired'")
    assert (audit["ref"], audit["actor"]) == ("mdt_video", "coordinator")
    assert [mandate(s5, ref)["status"] for ref in ("mdt_cloud", "mdt_fitness")] == [
        "ACTIVE",
        "ACTIVE",
    ]
    later = merchant_request(s5, "mdt_video", 2_500)
    assert (later["status"], later["reason_code"]) == ("AUTO_DECLINED", "MANDATE_EXPIRED")
    refused(change_response(s5, "mdt_video", "cancel"), 409, "MANDATE_CHANGED")


# Integrity (S5-SPEC §5.10)


@pytest.mark.case("T112")
def test_a_status_that_differs_from_its_event_fails_and_locks_while_reads_work(s5: Browser):
    waiting = merchant_request(s5, "mdt_cloud", 2_100)
    with s5.db.unit_of_work("test tamper") as uow:
        uow.execute("UPDATE mandates SET status = 'SUSPENDED' WHERE mandate_ref = 'mdt_video'")
    before = money(s5)

    response = change_response(s5, "mdt_video", "cancel")

    body = refused(response, 500, "INTEGRITY_VIOLATION")
    assert body["detail"]["reason"] == "mandate_event_mismatch"
    assert active_locks(s5) == [
        {
            "scope": "customer",
            "scope_ref": "cus_main",
            "reason": "INTEGRITY_VIOLATION: mandate_event_mismatch",
        }
    ]
    assert rows(s5, "SELECT * FROM actions") == [] and money(s5) == before
    assert len(mandate_events(s5, "mdt_video")) == 1
    # A merchant's request naming it fails the same way and is not stored.
    sent = s5.sim(
        "/sim/debit-requests",
        {
            "merchant_id": "mer_video",
            "merchant_request_id": "after-tamper",
            "mandate_ref": "mdt_video",
            "amount_fen": 2_500,
            "currency": "CNY",
        },
    )
    refused(sent, 500, "INTEGRITY_VIOLATION")
    assert len(rows(s5, "SELECT * FROM debit_requests")) == 1
    # Reads and operator reconciliation keep working.
    view = MandatesView.model_validate(s5.get("/v1/mandates").json())
    assert [m.mandate_ref for m in view.mandates if m.status.value == "SUSPENDED"] == ["mdt_video"]
    listed = s5.get("/v1/debit-requests").json()["requests"]
    assert [r["request_id"] for r in listed] == [waiting["request_id"]]
    s5.login("opr_ops")
    reconciled = s5.post("/v1/operator/reconcile", {"customer_id": "cus_main"}, role=Role.OPERATOR)
    assert reconciled.status_code == 200, reconciled.text
    assert len(active_locks(s5)) == 1


# Locks (T104)


@pytest.mark.case("T104")
def test_under_a_customer_lock_only_risk_reducing_changes_go_through(s5: Browser):
    video = merchant_request(s5, "mdt_video", 2_500)
    cloud = merchant_request(s5, "mdt_cloud", 2_100)
    open_pay = open_card(s5, draft(s5, cloud["request_id"]))
    with s5.db.unit_of_work("test lock") as uow:
        locks.set_lock(uow, LockScope.CUSTOMER, "cus_main", "test")
    before = money(s5)
    refused(confirm(s5, open_pay), 423, "LOCKED_WRITE")

    declined = complete(s5, open_card(s5, draft(s5, video["request_id"], "decline")))
    assert declined["operation"] is None
    assert request_row(s5, video["request_id"])["status"] == "DECLINED"
    for ref, kind, cap in (
        ("mdt_video", "set_cap", 2_000),
        ("mdt_cloud", "suspend", None),
        ("mdt_fitness", "cancel", None),
    ):
        done = change(s5, ref, kind, cap)
        assert done["operation"]["status"] == "SETTLED"
        assert receipt(s5, done["operation"]["operation_id"]).effect_check.value == "verified"
    assert mandate(s5, "mdt_video")["cap_fen"] == 2_000
    assert mandate(s5, "mdt_cloud")["status"] == "SUSPENDED"
    assert mandate(s5, "mdt_fitness")["status"] == "CANCELLED"

    actions_before = rows(s5, "SELECT logical_action_id, status FROM actions")
    refused(change_response(s5, "mdt_video", "set_cap", 2_500), 423, "LOCKED_WRITE")
    refused(change_response(s5, "mdt_cloud", "resume"), 423, "LOCKED_WRITE")
    third = merchant_request(s5, "mdt_video", 1_900)
    assert third["status"] == "OUT_OF_MANDATE"
    assert rows(s5, "SELECT logical_action_id, status FROM actions") == actions_before
    assert money(s5) == before and operations_of(s5, "DEBIT_PAY") == []
    assert len(active_locks(s5)) == 1
