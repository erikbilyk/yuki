"""S5 direct debits: opening a new agreement; T107 at layers D and H (S-layer part not here).

The case needs POST /v1/drafts/mandate, which is the optional level 4. When the build does not
list the route the tests are skipped with the reason "not built", and the case report shows that
reason instead of a pass. A case passes on stored agreements, actions, operations and the cash
snapshot. Every S5 contract name is a string here, so the module imports on a build without S5.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx
import pytest

from wg.contracts.api import ROUTES

from ..harness.client import WgClient
from .common import assert_cash
from .direct_debit import (
    AWAITING,
    EPOCH,
    action_count,
    approve,
    assert_subscriptions,
    body,
    has_direct_debits,
    mandates,
    merchant_request,
    merchants,
    money_state,
    operation_count,
    outcome,
    refused,
    reset_s5,
    stored_requests,
    subscription_periods,
)

has_create = any(r.method == "POST" and r.path == "/v1/drafts/mandate" for r in ROUTES)
pytestmark = pytest.mark.skipif(
    not (has_direct_debits() and has_create),
    reason="Not built: POST /v1/drafts/mandate (level 4) is not listed",
)

EXISTING = {"mdt_video", "mdt_cloud", "mdt_fitness"}


def create_response(wg: WgClient, merchant_id: str = "mer_music", **fields: Any) -> httpx.Response:
    request = {
        "merchant_id": merchant_id,
        "expected_fen": 1_500,
        "cap_fen": 2_000,
        "debit_day": 15,
        "months": 12,
        **fields,
    }
    return wg.post("/v1/drafts/mandate", request)


def assert_nothing_created(wg: WgClient, actions: int, operations: int) -> None:
    assert {m["mandate_ref"] for m in mandates(wg)["mandates"]} == EXISTING
    assert (action_count(wg), operation_count(wg)) == (actions, operations)


@pytest.mark.case("T107")
def test_t107_a_registry_merchant_gets_a_red_card_and_an_active_agreement(wg: WgClient) -> None:
    reset_s5(wg)
    before = money_state(wg)

    draft = body(create_response(wg), 201)
    assert draft["kind"] == "MANDATE_CREATE" and draft["tier"] == "RED", draft
    approved = approve(wg, draft)

    assert approved["card"]["requires_code"] is True and approved["verify"] is not None
    assert approved["operation"]["status"] == "SETTLED", approved["operation"]
    created = [m for m in mandates(wg)["mandates"] if m["mandate_ref"] not in EXISTING]
    assert len(created) == 1, mandates(wg)
    agreement = created[0]
    assert agreement["merchant_id"] == "mer_music" and agreement["status"] == "ACTIVE"
    assert (agreement["expected_fen"], agreement["cap_fen"], agreement["debit_day"]) == (
        1_500,
        2_000,
        15,
    )
    starts, expires = (
        date.fromisoformat(agreement["starts_on"]),
        date.fromisoformat(agreement["expires_on"]),
    )
    assert starts <= EPOCH.date() < expires <= starts + timedelta(days=366)
    assert mandates(wg)["monthly_total_fen"] == 24_500 + 1_500

    # The first period (due 11-15) is protected from the start; money is only held back.
    ref = agreement["mandate_ref"]
    assert subscription_periods(wg)[(ref, "2026-11")]["expected_fen"] == 1_500
    assert_subscriptions(wg, 24_500 + 1_500)
    assert_cash(wg, c=1_800_000, protected=1_624_500 + 1_500, headroom=175_500 - 1_500)
    assert money_state(wg)["posted"] == before["posted"]
    assert money_state(wg)["ledger"] == before["ledger"]

    # The merchant can now ask, and every request is still the customer's to answer.
    request = merchant_request(wg, ref, 1_500, merchant_id="mer_music")
    assert outcome(request) == (AWAITING, None), request


@pytest.mark.case("T107")
def test_t107_the_registry_lists_music_and_not_the_unverified_merchant(wg: WgClient) -> None:
    reset_s5(wg)

    listed = {m["merchant_id"] for m in merchants(wg)["merchants"]}

    assert listed == {"mer_video", "mer_cloud", "mer_fitness", "mer_music"}


@pytest.mark.case("T107")
def test_t107_an_unknown_merchant_is_not_found(wg: WgClient) -> None:
    reset_s5(wg)
    actions, operations = action_count(wg), operation_count(wg)

    refused(create_response(wg, "mer_nowhere"), 404, "NOT_FOUND")

    assert_nothing_created(wg, actions, operations)


@pytest.mark.case("T107")
def test_t107_an_unverified_merchant_is_refused(wg: WgClient) -> None:
    reset_s5(wg)
    actions, operations = action_count(wg), operation_count(wg)

    error = refused(create_response(wg, "mer_unverified"), 422, "UNSUPPORTED_REQUEST")

    assert error["detail"]["reason"] == "merchant_unverified", error
    assert_nothing_created(wg, actions, operations)


@pytest.mark.case("T107")
@pytest.mark.parametrize("months", [13, 24])
def test_t107_a_term_over_twelve_months_is_refused(wg: WgClient, months: int) -> None:
    reset_s5(wg)
    actions, operations = action_count(wg), operation_count(wg)

    error = refused(create_response(wg, months=months), 422, "UNSUPPORTED_REQUEST")

    assert error["detail"]["reason"] == "term_over_12_months", error
    assert_nothing_created(wg, actions, operations)


@pytest.mark.case("T107")
def test_t107_a_draft_alone_creates_no_agreement_and_no_protection(wg: WgClient) -> None:
    reset_s5(wg)

    draft = body(create_response(wg), 201)

    assert draft["kind"] == "MANDATE_CREATE"
    assert {m["mandate_ref"] for m in mandates(wg)["mandates"]} == EXISTING
    assert_subscriptions(wg, 24_500)
    assert stored_requests(wg) == []
    assert operation_count(wg) == 0
