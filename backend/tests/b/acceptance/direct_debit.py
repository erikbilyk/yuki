"""Helpers for the direct-debit cases (S5-ACCEPTANCE): requests and replies are plain JSON.

No S5-only contract class is imported at module level, so the S5 tests can be collected on a
build that has none; enum values are compared as strings. Names that exist on main (the client,
Role, ROUTES) are the only module-level imports from the product.
"""

from __future__ import annotations

import itertools
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from wg.contracts.api import ROUTES
from wg.contracts.enums import Role

from ..harness.client import MAIN_ACCOUNT, MAIN_CUSTOMER, WgClient, describe
from ..harness.inject import corrupt_unpaid

PROFILE = "fixture_s5"
CST = timezone(timedelta(hours=8))
EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=CST)
RESPOND_WITHIN = timedelta(hours=72)

VIDEO, CLOUD, FITNESS = "mdt_video", "mdt_cloud", "mdt_fitness"
OTHER_VIDEO = "mdt_other_video"
MERCHANT_OF = {
    VIDEO: "mer_video",
    CLOUD: "mer_cloud",
    FITNESS: "mer_fitness",
    OTHER_VIDEO: "mer_video",
}
# Expected amount and cap of each agreement of cus_main, in fen (S5-SPEC §10).
EXPECTED_FEN = {VIDEO: 2_500, CLOUD: 2_100, FITNESS: 19_900}
CAP_FEN = {VIDEO: 3_000, CLOUD: 2_100, FITNESS: 22_000}

AWAITING = "AWAITING_CLIENT"
OUT_OF_MANDATE = "OUT_OF_MANDATE"
AUTO_DECLINED = "AUTO_DECLINED"
PAID, DECLINED, EXPIRED = "PAID", "DECLINED", "EXPIRED"
CLEARING = "acc_sys_clearing"
RENT = "cmt_rent"

# Statuses of an action that no longer waits for the customer or the bank.
OPEN_ACTION_STATUSES = frozenset(
    {"DRAFT", "AWAITING_APPROVAL", "AWAITING_STEPUP", "READY", "SUBMITTED", "UNKNOWN"}
)

_serial = itertools.count(1)


def has_direct_debits() -> bool:
    """Whether the build lists the S5 routes; the S5 cases and the T59 segment key on this."""
    return any(r.method == "GET" and r.path == "/v1/mandates" for r in ROUTES)


def at(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A simulated instant of 2026 in CST."""
    return datetime(2026, month, day, hour, minute, tzinfo=CST)


def reset_s5(wg: WgClient, variant: str | None = None) -> None:
    """Load fixture_s5 (or one of its variants) and sign the customer in again."""
    result = wg.reset(variant, profile=PROFILE)
    assert result.profile == PROFILE, result
    wg.login(Role.CUSTOMER)


# Responses

DEMO = {"role": Role.DEMO}


def body(response: httpx.Response, *status: int) -> Any:
    """The parsed JSON of a response whose status is one of `status` (default 200)."""
    assert response.status_code in (status or (200,)), describe(response)
    return response.json()


def refused(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """The error body of a response with this status and error code."""
    assert response.status_code == status, describe(response)
    error = response.json()
    assert error.get("code") == code, describe(response)
    assert error.get("message_zh"), describe(response)
    return error


# The merchant's side: the simulator


def merchant_request_response(
    wg: WgClient,
    mandate_ref: str,
    amount_fen: Any,
    *,
    request_id: str | None = None,
    merchant_id: str | None = None,
    currency: str = "CNY",
    description: str = "",
) -> httpx.Response:
    """POST /sim/debit-requests as the demo role; the merchant defaults to the agreement's."""
    request_body = {
        "merchant_id": merchant_id or MERCHANT_OF.get(mandate_ref, "mer_video"),
        "merchant_request_id": request_id or f"mreq-{next(_serial):04d}",
        "mandate_ref": mandate_ref,
        "amount_fen": amount_fen,
        "currency": currency,
        "description_raw": description,
    }
    return wg.post("/sim/debit-requests", request_body, **DEMO)


def merchant_request(wg: WgClient, mandate_ref: str, amount_fen: int, **options: Any) -> dict:
    """One accepted-for-processing request; the reply is a SimDebitResult as JSON."""
    return body(merchant_request_response(wg, mandate_ref, amount_fen, **options))


# Reads


def mandates(wg: WgClient) -> dict[str, Any]:
    return body(wg.get("/v1/mandates"))


def mandate(wg: WgClient, mandate_ref: str) -> dict[str, Any]:
    found = [m for m in mandates(wg)["mandates"] if m["mandate_ref"] == mandate_ref]
    assert len(found) == 1, f"{len(found)} agreements named {mandate_ref}"
    return found[0]


def merchants(wg: WgClient) -> dict[str, Any]:
    return body(wg.get("/v1/merchants"))


def debit_requests(wg: WgClient, **query: str) -> list[dict[str, Any]]:
    """The customer's requests, newest first; `status` and `mandate_ref` narrow the list."""
    return body(wg.get("/v1/debit-requests", **query))["requests"]


def debit_request_response(wg: WgClient, request_id: str) -> httpx.Response:
    return wg.get(f"/v1/debit-requests/{request_id}")


def debit_request(wg: WgClient, request_id: str) -> dict[str, Any]:
    return body(debit_request_response(wg, request_id))


def subscription_spend(wg: WgClient, days: int | None = None) -> dict[str, Any]:
    return body(wg.get("/v1/subscriptions/spend", days=days))


def cash_details(wg: WgClient) -> dict[str, Any]:
    """The cash snapshot as JSON; `details.subscriptions` lists the protected periods."""
    return body(wg.get("/v1/cash/snapshot"))


def subscription_periods(wg: WgClient) -> dict[tuple[str, str], dict[str, Any]]:
    """Subscription periods of the snapshot by (mandate_ref, period_key)."""
    rows = cash_details(wg)["details"]["subscriptions"]
    return {(row["mandate_ref"], row["period_key"]): row for row in rows}


def receipt(wg: WgClient, operation_id: str) -> dict[str, Any]:
    return body(wg.get(f"/v1/receipts/{operation_id}"))


def effect_check(wg: WgClient, operation_id: str) -> str | None:
    """`effect_check` of the receipt: verified, pending or failed; null for other kinds."""
    return receipt(wg, operation_id).get("effect_check")


# Drafts, cards and codes (the customer's path)


def decision_draft_response(wg: WgClient, request_id: str, decision: str) -> httpx.Response:
    return wg.post("/v1/drafts/debit-decision", {"request_id": request_id, "decision": decision})


def draft_decision(wg: WgClient, request_id: str, decision: str) -> dict[str, Any]:
    return body(decision_draft_response(wg, request_id, decision), 201)


def change_draft_response(
    wg: WgClient, mandate_ref: str, change: str, cap_fen: int | None = None
) -> httpx.Response:
    request_body: dict[str, Any] = {"mandate_ref": mandate_ref, "change": change}
    if cap_fen is not None:
        request_body["cap_fen"] = cap_fen
    return wg.post("/v1/drafts/mandate-change", request_body)


def draft_change(
    wg: WgClient, mandate_ref: str, change: str, cap_fen: int | None = None
) -> dict[str, Any]:
    return body(change_draft_response(wg, mandate_ref, change, cap_fen), 201)


def open_card(wg: WgClient, draft: dict[str, Any]) -> dict[str, Any]:
    """Ask for approval and read the card the trusted UI would show."""
    approval = body(wg.post(f"/v1/actions/{draft['logical_action_id']}/request-approval"))
    return body(wg.get(f"/v1/cards/{approval['card_id']}"))


def confirm_card_response(wg: WgClient, card: dict[str, Any], **extra: Any) -> httpx.Response:
    return wg.post(
        f"/v1/cards/{card['card_id']}/confirm", {"terms_hash": card["terms_hash"], **extra}
    )


def sms_code(wg: WgClient, challenge_id: str) -> str:
    """The code the simulated phone shows for an open challenge."""
    messages = body(wg.get("/v1/sms"))["messages"]
    codes = [m["code"] for m in messages if m["challenge_id"] == challenge_id]
    assert len(codes) == 1, f"expected one SMS for {challenge_id}, found {len(codes)}"
    return codes[0]


def verify_response(wg: WgClient, challenge_id: str, code: str | None = None) -> httpx.Response:
    return wg.post(
        f"/v1/challenges/{challenge_id}/verify", {"code": code or sms_code(wg, challenge_id)}
    )


def approve(wg: WgClient, draft: dict[str, Any]) -> dict[str, Any]:
    """Card, confirm and, for a RED card, the SMS code. Returns the steps and the operation.

    Keys: draft, card, confirm, verify (None for a YELLOW card) and operation (None when the
    action is a state change without an operation, such as a decline).
    """
    return complete(wg, draft, open_card(wg, draft))


def complete(wg: WgClient, draft: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    """Confirm an open card and, for a RED card, verify its SMS code; the keys of `approve`."""
    confirm = body(confirm_card_response(wg, card), 200, 201, 202)
    verify = None
    if confirm.get("challenge") is not None:
        verify = body(verify_response(wg, confirm["challenge"]["challenge_id"]), 200, 201, 202)
    final = verify if verify is not None else confirm
    return {
        "draft": draft,
        "card": card,
        "confirm": confirm,
        "verify": verify,
        "operation": final.get("operation"),
    }


def pay_request(wg: WgClient, request_id: str) -> dict[str, Any]:
    """The customer pays a request: draft, card, confirm."""
    return approve(wg, draft_decision(wg, request_id, "pay"))


def decline_request(wg: WgClient, request_id: str) -> dict[str, Any]:
    return approve(wg, draft_decision(wg, request_id, "decline"))


def change_agreement(
    wg: WgClient, mandate_ref: str, change: str, cap_fen: int | None = None
) -> dict[str, Any]:
    return approve(wg, draft_change(wg, mandate_ref, change, cap_fen))


# The database, read-only (S5-SPEC §4.2)


def stored_requests(wg: WgClient, mandate_ref: str | None = None) -> list[dict[str, Any]]:
    """Every row of debit_requests, probes included, oldest first."""
    rows = wg.db.rows("SELECT * FROM debit_requests ORDER BY received_at, request_id")
    return [r for r in rows if mandate_ref is None or r["mandate_ref"] == mandate_ref]


def stored_mandate(wg: WgClient, mandate_ref: str) -> dict[str, Any]:
    rows = wg.db.rows("SELECT * FROM mandates WHERE mandate_ref = ?", (mandate_ref,))
    assert len(rows) == 1, f"{len(rows)} rows for {mandate_ref}"
    return rows[0]


def mandate_events(wg: WgClient, mandate_ref: str) -> list[dict[str, Any]]:
    return wg.db.rows(
        "SELECT * FROM mandate_events WHERE mandate_ref = ? ORDER BY event_id", (mandate_ref,)
    )


def effect_events(wg: WgClient, operation_id: str) -> list[dict[str, Any]]:
    return wg.db.rows(
        "SELECT * FROM effect_verification_events WHERE operation_id = ? ORDER BY event_id",
        (operation_id,),
    )


def audit_events(wg: WgClient, event: str) -> list[dict[str, Any]]:
    return wg.db.rows("SELECT * FROM audit_log WHERE event = ? ORDER BY audit_id", (event,))


def action_count(wg: WgClient) -> int:
    return wg.db.count("actions")


def operation_count(wg: WgClient) -> int:
    return wg.db.count("operations")


def assert_period_rules(wg: WgClient) -> None:
    """Row-level invariants of S5-SPEC §4.2 that no request sequence may break."""
    rows = [r for r in stored_requests(wg) if not r["probe"]]
    ids = [(r["merchant_id"], r["merchant_request_id"]) for r in rows]
    assert len(ids) == len(set(ids)), "a merchant request id is stored twice"
    occupying = Counter(
        (r["mandate_ref"], r["period_key"])
        for r in rows
        if r["status"] in (AWAITING, PAID, DECLINED)
    )
    assert all(n == 1 for n in occupying.values()), f"a period is used twice: {occupying}"
    paid = Counter((r["mandate_ref"], r["period_key"]) for r in rows if r["status"] == PAID)
    assert all(n == 1 for n in paid.values()), f"a period is paid twice: {paid}"


# The sweep of T59


def sweep_direct_debit_routes(wg: WgClient) -> None:
    """Call every S5 route the build lists, each with a success, and one format error.

    Failures are as welcome as successes; the caller scans every response.
    """
    listed = {(r.method, r.path) for r in ROUTES}
    ghost = "nothingthere0001"
    reset_s5(wg)

    if ("GET", "/v1/merchants") in listed:
        wg.get("/v1/merchants")
    if ("GET", "/v1/mandates") in listed:
        wg.get("/v1/mandates")

    posted = merchant_request_response(wg, VIDEO, 2_500, description="模拟附言")
    request_id = body(posted)["request_id"]
    merchant_request_response(wg, VIDEO, 2_500, request_id="mreq-t59-repeat")
    merchant_request_response(wg, VIDEO, 2_500, request_id="mreq-t59-repeat")
    merchant_request_response(wg, CLOUD, 2_500)
    merchant_request_response(wg, "mdt_unknown", 2_500, merchant_id="mer_unknown")
    refused(merchant_request_response(wg, CLOUD, 0), 422, "FORMAT_ERROR")
    merchant_request_response(wg, CLOUD, 25.5)
    merchant_request_response(wg, CLOUD, 2_100, currency="USD")

    if ("GET", "/v1/debit-requests") in listed:
        wg.get("/v1/debit-requests")
        wg.get("/v1/debit-requests", status=AWAITING, mandate_ref=VIDEO)
    if ("GET", "/v1/debit-requests/{id}") in listed:
        debit_request_response(wg, request_id)
        debit_request_response(wg, f"req_{ghost}")
    if ("GET", "/v1/subscriptions/spend") in listed:
        wg.get("/v1/subscriptions/spend")
        wg.get("/v1/subscriptions/spend", days=7)
        wg.get("/v1/subscriptions/spend", days=0)

    if ("POST", "/v1/drafts/debit-decision") in listed:
        decision_draft_response(wg, request_id, "pay")
        decision_draft_response(wg, request_id, "pay")
        decision_draft_response(wg, request_id, "decline")
        decision_draft_response(wg, f"req_{ghost}", "pay")
        wg.post("/v1/drafts/debit-decision", {"request_id": request_id, "decision": "maybe"})
    if ("POST", "/v1/drafts/mandate-change") in listed:
        change_draft_response(wg, FITNESS, "set_cap", 20_000)
        change_draft_response(wg, FITNESS, "set_cap", CAP_FEN[FITNESS])
        change_draft_response(wg, FITNESS, "set_cap")
        change_draft_response(wg, CLOUD, "suspend")
        change_draft_response(wg, f"mdt_{ghost}", "cancel")
    if ("POST", "/v1/drafts/mandate") in listed:
        wg.post(
            "/v1/drafts/mandate",
            {
                "merchant_id": "mer_music",
                "expected_fen": 1_500,
                "cap_fen": 2_000,
                "debit_day": 15,
                "months": 12,
            },
        )
        wg.post(
            "/v1/drafts/mandate",
            {
                "merchant_id": "mer_unverified",
                "expected_fen": 1_500,
                "cap_fen": 2_000,
                "debit_day": 15,
                "months": 12,
            },
        )


def race(wg: WgClient, sends: list[Callable[[WgClient], httpx.Response]]) -> list[httpx.Response]:
    """Run each send on its own connection (another tab) released together."""
    tabs = [wg.fork() for _ in sends]
    start = threading.Barrier(len(tabs))

    def run(tab: WgClient, send: Callable[[WgClient], httpx.Response]) -> httpx.Response:
        start.wait()
        return send(tab)

    try:
        with ThreadPoolExecutor(len(tabs)) as pool:
            return list(pool.map(run, tabs, sends))
    finally:
        for tab in tabs:
            tab.close()


# Money, protection and receipts


def subscription_sum(wg: WgClient) -> int:
    """Fen protected for subscription periods in the cash snapshot."""
    return sum(row["expected_fen"] for row in subscription_periods(wg).values())


def assert_subscriptions(wg: WgClient, expected_fen: int) -> dict[tuple[str, str], dict[str, Any]]:
    """The protected subscription sum, and B as the two unpaid bills plus that sum.

    The second check holds while rent and insurance are unpaid (S5-ACCEPTANCE §3).
    """
    periods = subscription_periods(wg)
    assert sum(row["expected_fen"] for row in periods.values()) == expected_fen, periods
    details = cash_details(wg)["details"]
    assert details["b_fen"] == 840_000 + expected_fen, details
    return periods


def money_state(wg: WgClient) -> dict[str, Any]:
    """Everything a refused or ineffective step must leave as it was."""
    return {
        "posted": wg.balance().posted_fen,
        "ledger": wg.db.count("ledger_entries"),
        "reservations": wg.db.reservations(),
        "payments": payment_operations(wg),
        "caps": {m["mandate_ref"]: m["cap_fen"] for m in mandates(wg)["mandates"]},
    }


def payment_operations(wg: WgClient) -> list[dict[str, Any]]:
    return wg.db.rows("SELECT * FROM operations WHERE kind = 'DEBIT_PAY' ORDER BY rowid")


def operation_kinds(wg: WgClient) -> list[str]:
    """Kinds of every operation in the order they were stored."""
    return [r["kind"] for r in wg.db.rows("SELECT kind FROM operations ORDER BY rowid")]


def assert_paid_once(
    wg: WgClient, request_id: str, amount_fen: int, mandate_ref: str = VIDEO
) -> dict[str, Any]:
    """The only DEBIT_PAY operation settled with its receipt, ledger entries and a PAID request.

    Returns the stored operation row.
    """
    request = debit_request(wg, request_id)
    assert request["status"] == PAID and request["payable"] is False, request
    operations = payment_operations(wg)
    assert len(operations) == 1, f"{len(operations)} DEBIT_PAY operations"
    operation = operations[0]
    assert request["operation_id"] == operation["operation_id"]
    assert operation["status"] == "SETTLED" and operation["amount_fen"] == amount_fen
    assert (operation["customer_id"], operation["account_id"]) == (MAIN_CUSTOMER, MAIN_ACCOUNT)

    entries = wg.db.ledger_entries(operation["operation_id"])
    assert sum(e["amount_fen"] for e in entries) == 0
    assert sorted((e["account_id"], e["amount_fen"]) for e in entries) == sorted(
        [(MAIN_ACCOUNT, -amount_fen), (CLEARING, amount_fen)]
    )
    stored = wg.db.receipts(operation["operation_id"])
    assert len(stored) == 1 and stored[0]["status"] == "SETTLED"
    shown = receipt(wg, operation["operation_id"])
    assert shown["receipt_id"] == stored[0]["receipt_id"]
    assert (shown["kind"], shown["status"], shown["amount_fen"]) == (
        "DEBIT_PAY",
        "SETTLED",
        amount_fen,
    )
    assert (shown["mandate_ref"], shown["request_id"]) == (mandate_ref, request_id)
    assert shown["effect_check"] is None
    assert [r["status"] for r in stored_requests(wg, mandate_ref)].count(PAID) == 1
    return operation


# A customer lock by the T21 corruption


def lock_customer(wg: WgClient) -> None:
    """Corrupt the rent's unpaid amount and try to pay it: the refusal locks cus_main.

    Leaves an operator session next to the customer's, so the locks can be read.
    """
    wg.login(Role.OPERATOR)
    corrupt_unpaid(wg.db.path, RENT, 300_000)
    draft = wg.pay_bill_response(RENT, 50_000)
    refusal = draft
    if draft.status_code == 201:
        approval = wg.post(f"/v1/actions/{draft.json()['logical_action_id']}/request-approval")
        refusal = approval
        if approval.status_code < 400:
            card = wg.card(approval.json()["card_id"])
            refusal = wg.confirm_response(card)
    assert refusal.status_code >= 400, "a payment ran on a bill whose unpaid amount has no payments"
    assert refusal.json()["code"] in ("INTEGRITY_VIOLATION", "LOCKED_WRITE"), describe(refusal)
    assert [(lock.scope, lock.scope_ref) for lock in wg.locks().locks] == [
        ("customer", MAIN_CUSTOMER)
    ]


# Reading stored requests


def outcome(result: dict[str, Any]) -> tuple[str, str | None]:
    """Status and reason code of a SimDebitResult."""
    return result["status"], result["reason_code"]


def probes(wg: WgClient, mandate_ref: str) -> list[dict[str, Any]]:
    return [r for r in stored_requests(wg, mandate_ref) if r["probe"]]


def customer_requests(wg: WgClient, mandate_ref: str) -> list[dict[str, Any]]:
    return [r for r in stored_requests(wg, mandate_ref) if not r["probe"]]


def operations_of(wg: WgClient, kind: str) -> list[dict[str, Any]]:
    return wg.db.rows("SELECT * FROM operations WHERE kind = ? ORDER BY rowid", (kind,))
