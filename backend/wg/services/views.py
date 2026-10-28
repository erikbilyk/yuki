"""Response bodies built from stored rows: operations, receipts, transfers, accounts."""

from __future__ import annotations

from wg.contracts.api import (
    AccountView,
    OperationView,
    PayeeView,
    ReceiptView,
    TransferView,
)
from wg.contracts.enums import ActionKind
from wg.contracts.errors import ErrorCode
from wg.core.k1_session import payee_handle
from wg.k6 import accounts, actions, operations
from wg.k6.accounts import AccountRow, PayeeRow
from wg.k6.db import Reader
from wg.k6.operations import OperationRow, ReceiptRow


def operation_view(reader: Reader, operation: OperationRow) -> OperationView:
    assert operation.logical_action_id is not None and operation.revision is not None
    action = actions.get_revision(reader, operation.logical_action_id, operation.revision)
    receipt = operations.receipt_of(reader, operation.operation_id)
    return OperationView(
        operation_id=operation.operation_id,
        logical_action_id=operation.logical_action_id,
        revision=operation.revision,
        plan_id=action.plan_id,
        kind=operation.action_kind,
        status=operation.status,
        account_id=operation.account_id,
        amount_fen=operation.amount_fen,
        shares=operation.shares,
        product_id=operation.product_id,
        payee_id=operation.payee_id,
        commitment_id=operation.commitment_id,
        submitted_at=operation.submitted_at,
        settle_due_at=operation.settle_due_at,
        settled_at=operation.settled_at,
        receipt_id=receipt.receipt_id if receipt else None,
    )


def receipt_view(reader: Reader, operation: OperationRow, receipt: ReceiptRow) -> ReceiptView:
    assert operation.logical_action_id is not None and operation.revision is not None
    return ReceiptView(
        receipt_id=receipt.receipt_id,
        operation_id=operation.operation_id,
        logical_action_id=operation.logical_action_id,
        revision=operation.revision,
        status=receipt.status,
        kind=operation.action_kind,
        account_id=operation.account_id,
        amount_fen=operation.amount_fen,
        shares=operation.shares,
        product_id=operation.product_id,
        payee_id=operation.payee_id,
        payee_display=payee_display(reader, operation.payee_id),
        commitment_id=operation.commitment_id,
        reason_code=_error_code(receipt.reason_code),
        created_at=receipt.created_at,
    )


def transfer_view(reader: Reader, operation: OperationRow) -> TransferView:
    assert operation.logical_action_id is not None and operation.payee_id is not None
    receipt = operations.receipt_of(reader, operation.operation_id)
    display = payee_display(reader, operation.payee_id)
    assert display is not None
    return TransferView(
        operation_id=operation.operation_id,
        logical_action_id=operation.logical_action_id,
        kind=ActionKind(operation.kind),
        account_id=operation.account_id,
        payee_id=operation.payee_id,
        payee_display=display,
        amount_fen=operation.amount_fen,
        commitment_id=operation.commitment_id,
        status=operation.status,
        submitted_at=operation.submitted_at,
        settled_at=operation.settled_at,
        receipt_id=receipt.receipt_id if receipt else None,
    )


def account_view(reader: Reader, account: AccountRow) -> AccountView:
    return AccountView(
        account_id=account.account_id,
        name=account.name,
        posted_fen=account.balance_fen,
        available_fen=account.balance_fen - operations.reserved_fen(reader, account.account_id),
        covered=account.covered,
    )


def payee_view(payee: PayeeRow) -> PayeeView:
    return PayeeView(
        payee_id=payee.payee_id,
        alias=payee.alias,
        display_name=payee.display_name,
        bank=payee.bank,
        account_masked=payee.account_masked,
    )


def payee_display(reader: Reader, payee_id: str | None) -> str | None:
    if payee_id is None:
        return None
    payee = accounts.find_payee(reader, payee_id)
    if payee is None:
        return None
    return payee_handle(payee.alias, payee.bank, payee.account_masked)


def _error_code(value: str | None) -> ErrorCode | None:
    """A stored reason that is one of the error codes; bank reasons stay in the receipt effect."""
    if value is None or value not in ErrorCode.__members__:
        return None
    return ErrorCode(value)
