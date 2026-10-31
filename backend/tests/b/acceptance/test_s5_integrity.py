"""S5 direct debits: an agreement whose status disagrees with its newest event; T112 at layer D.

The test edits `mandates.status` directly, which no route can do. The next write that names the
agreement then fails with INTEGRITY_VIOLATION (mandate_event_mismatch) and locks the customer;
reads and operator reconciliation keep working (S5-SPEC §5.10). A case passes on stored requests,
actions, operations, locks and failure records. Every S5 contract name is a string here, so the
module imports on a build without S5.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from wg.contracts.enums import Role

from ..harness.client import MAIN_CUSTOMER, WgClient
from ..harness.inject import set_mandate_status
from .common import active_locks
from .direct_debit import (
    AWAITING,
    CLOUD,
    VIDEO,
    action_count,
    audit_events,
    change_draft_response,
    debit_request,
    debit_requests,
    decision_draft_response,
    has_direct_debits,
    mandate,
    mandate_events,
    mandates,
    merchant_request,
    merchant_request_response,
    money_state,
    operation_count,
    refused,
    reset_s5,
    stored_mandate,
    stored_requests,
)

pytestmark = pytest.mark.skipif(
    not has_direct_debits(), reason="the build lists no direct-debit routes"
)

REASON = "mandate_event_mismatch"


def tamper(wg: WgClient, mandate_ref: str, status: str) -> None:
    """Make the stored status differ from the newest status event, as only a defect could."""
    newest = [e for e in mandate_events(wg, mandate_ref) if e["field"] == "status"][-1]
    assert newest["new"] != status, "the edit must create a difference"
    set_mandate_status(wg.db.path, mandate_ref, status)
    assert stored_mandate(wg, mandate_ref)["status"] == status


def refused_for_integrity(response: httpx.Response, wg: WgClient) -> None:
    refused(response, 500, "INTEGRITY_VIOLATION")
    failures = [f"{row['error_class']} {row['detail']}" for row in wg.db.failure_records()]
    reasons = [lock.reason for lock in active_locks(wg)]
    assert any(REASON in text for text in failures + reasons + [response.text]), (
        failures,
        reasons,
    )


def assert_locked_and_untouched(
    wg: WgClient, before: dict[str, Any], actions: int, operations: int
) -> None:
    assert [(lock.scope, lock.scope_ref) for lock in active_locks(wg)] == [
        ("customer", MAIN_CUSTOMER)
    ]
    assert money_state(wg) == before
    assert (action_count(wg), operation_count(wg)) == (actions, operations)


@pytest.mark.case("T112")
@pytest.mark.layer("D")
@pytest.mark.parametrize("status", ["SUSPENDED", "CANCELLED", "EXPIRED"])
def test_t112_a_merchant_request_naming_the_agreement_fails_and_locks(
    app_client: WgClient, status: str
) -> None:
    wg = app_client
    reset_s5(wg)
    wg.login(Role.OPERATOR)
    tamper(wg, VIDEO, status)
    before, actions, operations = money_state(wg), action_count(wg), operation_count(wg)

    response = merchant_request_response(wg, VIDEO, 2_500)

    refused_for_integrity(response, wg)
    assert stored_requests(wg) == [], "a request was stored for an agreement that fails the check"
    for event in ("debit_request_received", "debit_request_refused"):
        assert audit_events(wg, event) == []
    assert_locked_and_untouched(wg, before, actions, operations)


@pytest.mark.case("T112")
@pytest.mark.layer("D")
@pytest.mark.parametrize("change", ["cancel", "suspend"])
def test_t112_a_change_draft_fails_even_when_it_reduces_risk(
    app_client: WgClient, change: str
) -> None:
    wg = app_client
    reset_s5(wg)
    wg.login(Role.OPERATOR)
    tamper(wg, CLOUD, "CANCELLED")
    before, actions, operations = money_state(wg), action_count(wg), operation_count(wg)

    response = change_draft_response(wg, CLOUD, change)

    refused_for_integrity(response, wg)
    assert_locked_and_untouched(wg, before, actions, operations)
    events = [e for e in mandate_events(wg, CLOUD) if e["field"] == "status"]
    assert [e["new"] for e in events] == ["ACTIVE"], "the refused draft appended an event"


@pytest.mark.case("T112")
@pytest.mark.layer("D")
@pytest.mark.parametrize("decision", ["pay", "decline"])
def test_t112_a_decision_draft_fails(app_client: WgClient, decision: str) -> None:
    wg = app_client
    reset_s5(wg)
    wg.login(Role.OPERATOR)
    request = merchant_request(wg, VIDEO, 2_500)
    assert request["status"] == AWAITING
    tamper(wg, VIDEO, "SUSPENDED")
    before, actions, operations = money_state(wg), action_count(wg), operation_count(wg)

    response = decision_draft_response(wg, request["request_id"], decision)

    refused_for_integrity(response, wg)
    assert_locked_and_untouched(wg, before, actions, operations)
    assert debit_request(wg, request["request_id"])["status"] == AWAITING


@pytest.mark.case("T112")
@pytest.mark.layer("D")
def test_t112_reads_and_operator_reconciliation_keep_working(app_client: WgClient) -> None:
    wg = app_client
    reset_s5(wg)
    wg.login(Role.OPERATOR)
    request = merchant_request(wg, CLOUD, 2_100)
    tamper(wg, VIDEO, "CANCELLED")
    refused_for_integrity(merchant_request_response(wg, VIDEO, 2_500), wg)
    before = money_state(wg)

    listed = mandates(wg)["mandates"]
    assert sorted(m["mandate_ref"] for m in listed) == sorted(
        ["mdt_video", "mdt_cloud", "mdt_fitness"]
    )
    assert mandate(wg, VIDEO)["status"] == "CANCELLED"
    assert [r["request_id"] for r in debit_requests(wg)] == [request["request_id"]]
    wg.snapshot()
    wg.reconcile(MAIN_CUSTOMER)

    assert [(lock.scope, lock.scope_ref) for lock in active_locks(wg)] == [
        ("customer", MAIN_CUSTOMER)
    ]
    assert money_state(wg) == before
