"""S5 direct debits: the payment when the response is lost; T105 at layer K.

The customer confirms a payment card while the process ends before or after COMMIT (by crash or
pause and kill), or while the bank's acknowledgement is lost. After the restart the client repeats
the confirm it never saw answered. A case passes on one DEBIT_PAY operation with its original
receipt, one PAID request and one pair of ledger entries. Every S5 contract name is a string
here, so the module imports on a build without S5.
"""

from __future__ import annotations

import pytest

from wg.contracts.enums import Fault, Role

from ..conftest import Live
from ..harness.client import MAIN_ACCOUNT, MAIN_CUSTOMER, describe
from .common import AFTER_COMMIT, LOST_RESPONSE_FAULTS, assert_cash, lose_response
from .direct_debit import (
    AWAITING,
    CLEARING,
    PAID,
    VIDEO,
    assert_paid_once,
    assert_period_rules,
    confirm_card_response,
    debit_request,
    draft_decision,
    has_direct_debits,
    merchant_request,
    open_card,
    payment_operations,
    reset_s5,
)

pytestmark = pytest.mark.skipif(
    not has_direct_debits(), reason="the build lists no direct-debit routes"
)

START_FEN = 1_800_000
PAYMENT_FEN = 2_500


# T105


@pytest.mark.case("T105")
@pytest.mark.parametrize("fault", LOST_RESPONSE_FAULTS, ids=[f.value for f in LOST_RESPONSE_FAULTS])
def test_t105_lost_response_on_the_payment_and_restart(live: Live, fault: Fault) -> None:
    client = live.client
    reset_s5(client)
    request = merchant_request(client, VIDEO, PAYMENT_FEN)
    request_id = request["request_id"]
    draft = draft_decision(client, request_id, "pay")
    card = open_card(client, draft)
    client.arm(fault)

    lose_response(live, fault, lambda: confirm_card_response(client, card))
    live.restart()

    committed = payment_operations(client)
    original_receipt = None
    if fault in AFTER_COMMIT:
        assert len(committed) == 1, "the committed payment is missing after restart"
        assert debit_request(client, request_id)["status"] == PAID
        original_receipt = client.db.receipts(committed[0]["operation_id"])[0]["receipt_id"]
    else:
        # Restart recovery may already have resubmitted the READY action.
        assert len(committed) <= 1
        if not committed:
            assert debit_request(client, request_id)["status"] == AWAITING
            assert client.balance().posted_fen == START_FEN

    # The client repeats the request whose response it never received.
    retry = confirm_card_response(client, card)
    if fault in AFTER_COMMIT:
        assert retry.status_code == 200, describe(retry)
        assert retry.json()["operation"]["operation_id"] == committed[0]["operation_id"]
    else:
        assert retry.status_code in (200, 201), describe(retry)

    operation = assert_paid_once(client, request_id, PAYMENT_FEN)
    if original_receipt is not None:
        assert client.db.receipts(operation["operation_id"])[0]["receipt_id"] == original_receipt
    assert client.balance().posted_fen == START_FEN - PAYMENT_FEN
    assert len(client.db.ledger_entries(operation["operation_id"])) == 2
    assert_cash(client, c=1_797_500, protected=1_622_000, headroom=175_500)
    assert_period_rules(client)


@pytest.mark.case("T105")
def test_t105_a_lost_bank_acknowledgement_is_settled_by_reconciliation(live: Live) -> None:
    client = live.client
    reset_s5(client)
    request = merchant_request(client, VIDEO, PAYMENT_FEN)
    request_id = request["request_id"]
    draft = draft_decision(client, request_id, "pay")
    card = open_card(client, draft)
    client.arm(Fault.BANK_ACK_LOST)

    response = confirm_card_response(client, card)

    assert response.status_code == 202, describe(response)
    (unknown,) = payment_operations(client)
    operation_id, key = unknown["operation_id"], unknown["idempotency_key"]
    assert unknown["status"] == "UNKNOWN"
    assert client.db.action_status(draft["logical_action_id"]) == "UNKNOWN"
    # The bank has the order: the request is paid and the money has moved, but no receipt yet.
    assert debit_request(client, request_id)["status"] == PAID
    assert len(client.db.bank_requests(key)) == 1
    entries = client.db.ledger_entries(operation_id)
    assert sorted((e["account_id"], e["amount_fen"]) for e in entries) == sorted(
        [(MAIN_ACCOUNT, -PAYMENT_FEN), (CLEARING, PAYMENT_FEN)]
    )
    assert client.db.receipts(operation_id) == []
    assert client.balance().posted_fen == START_FEN - PAYMENT_FEN

    client.login(Role.OPERATOR)
    client.reconcile(MAIN_CUSTOMER)

    # The same operation and key, now with its one receipt; nothing was paid twice.
    settled = assert_paid_once(client, request_id, PAYMENT_FEN)
    assert settled["operation_id"] == operation_id and settled["idempotency_key"] == key
    assert len(client.db.bank_requests(key)) == 1
    assert client.db.ledger_entries(operation_id) == entries
    assert "RECONCILED" in [e["event"] for e in client.db.operation_events(operation_id)]
    assert client.balance().posted_fen == START_FEN - PAYMENT_FEN
    assert_period_rules(client)
