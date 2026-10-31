"""The customer's and the merchant's steps for the direct-debit HTTP tests."""

from __future__ import annotations

from typing import Any

from wg.contracts.api import Card, DraftResult

from .conftest import Browser

MERCHANT_OF = {"mdt_video": "mer_video", "mdt_cloud": "mer_cloud", "mdt_fitness": "mer_fitness"}
_serial = iter(range(1, 10_000))


def merchant_request(browser: Browser, ref: str = "mdt_video", amount: int = 2_500, **extra):
    response = browser.sim(
        "/sim/debit-requests",
        {
            "merchant_id": MERCHANT_OF[ref],
            "merchant_request_id": f"d-{next(_serial)}",
            "mandate_ref": ref,
            "amount_fen": amount,
            "currency": "CNY",
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def decision_response(browser: Browser, request_id: str, decision: str):
    return browser.post(
        "/v1/drafts/debit-decision", {"request_id": request_id, "decision": decision}
    )


def draft(browser: Browser, request_id: str, decision: str = "pay") -> DraftResult:
    response = decision_response(browser, request_id, decision)
    assert response.status_code == 201, response.text
    return DraftResult.model_validate(response.json())


def open_card(browser: Browser, result: DraftResult) -> Card:
    approval = browser.post(f"/v1/actions/{result.logical_action_id}/request-approval")
    assert approval.status_code == 200, approval.text
    response = browser.get(f"/v1/cards/{approval.json()['card_id']}")
    assert response.status_code == 200, response.text
    return Card.model_validate(response.json())


def confirm(browser: Browser, card: Card):
    return browser.post(f"/v1/cards/{card.card_id}/confirm", {"terms_hash": card.terms_hash})


def complete(browser: Browser, card: Card) -> dict[str, Any]:
    """Confirm the card and, when it asks for one, enter the SMS code."""
    response = confirm(browser, card)
    assert response.status_code in (200, 201, 202), response.text
    body = response.json()
    if body["challenge"] is None:
        return body
    challenge_id = body["challenge"]["challenge_id"]
    (code,) = [
        m["code"]
        for m in browser.get("/v1/sms").json()["messages"]
        if m["challenge_id"] == challenge_id
    ]
    verified = browser.post(f"/v1/challenges/{challenge_id}/verify", {"code": code})
    assert verified.status_code in (200, 201, 202), verified.text
    return verified.json()


def pay(browser: Browser, request_id: str) -> dict[str, Any]:
    return complete(browser, open_card(browser, draft(browser, request_id)))
