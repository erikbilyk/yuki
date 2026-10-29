"""Reads of the customer's own records: accounts, payees, transfers, operations, receipts.

A foreign and a missing record give the same NOT_FOUND (SPEC §6, T54).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from wg.contracts.api import (
    AccountsView,
    BalanceView,
    OperationView,
    PayeesView,
    ReceiptView,
    TransferHistoryView,
)
from wg.contracts.tools import OperationStatusResult
from wg.core.k1_session import not_found, owned
from wg.core.k3_policy import DAILY_SUM_KINDS
from wg.k6 import accounts, actions, operations
from wg.k6.db import Database
from wg.k6.simtime import now
from wg.services.views import account_view, operation_view, payee_view, receipt_view, transfer_view

MAX_HISTORY_DAYS = 90


def _owner(record: Any) -> str | None:
    return record.customer_id


class ReadService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def accounts(self, customer_id: str) -> AccountsView:
        with self.db.read() as conn:
            rows = accounts.customer_accounts(conn, customer_id)
            return AccountsView(accounts=tuple(account_view(conn, row) for row in rows))

    def balance(self, customer_id: str, account_id: str) -> BalanceView:
        with self.db.read() as conn:
            account = owned(accounts.find_account(conn, account_id), customer_id, _owner)
            reserved = operations.reserved_fen(conn, account.account_id)
            return BalanceView(
                account_id=account.account_id,
                name=account.name,
                posted_fen=account.balance_fen,
                reserved_fen=reserved,
                available_fen=account.balance_fen - reserved,
                as_of=now(conn),
            )

    def payees(self, customer_id: str, query: str | None = None) -> PayeesView:
        with self.db.read() as conn:
            rows = accounts.customer_payees(conn, customer_id, query)
        return PayeesView(payees=tuple(payee_view(row) for row in rows))

    def history(
        self, customer_id: str, days: int = 30, payee_id: str | None = None
    ) -> TransferHistoryView:
        """Transfers and bill payments submitted in the last `days` (at most 90)."""
        days = min(days, MAX_HISTORY_DAYS)
        with self.db.read() as conn:
            if payee_id is not None:
                owned(accounts.find_payee(conn, payee_id), customer_id, _owner)
            rows = operations.customer_operations(
                conn,
                customer_id,
                kinds=sorted(DAILY_SUM_KINDS),
                since=now(conn) - timedelta(days=days),
                payee_id=payee_id,
            )
            return TransferHistoryView(
                days=days, transfers=tuple(transfer_view(conn, row) for row in rows)
            )

    def operation(self, customer_id: str, operation_id: str) -> OperationView:
        with self.db.read() as conn:
            operation = owned(operations.find_operation(conn, operation_id), customer_id, _owner)
            if operation.logical_action_id is None:
                raise not_found()
            return operation_view(conn, operation)

    def receipt(self, customer_id: str, operation_id: str) -> ReceiptView:
        """The terminal receipt; an operation still SUBMITTED or UNKNOWN has none yet."""
        with self.db.read() as conn:
            operation = owned(operations.find_operation(conn, operation_id), customer_id, _owner)
            receipt = operations.receipt_of(conn, operation_id)
            if receipt is None or operation.logical_action_id is None:
                raise not_found()
            return receipt_view(conn, operation, receipt)

    def action_status(
        self,
        customer_id: str,
        logical_action_id: str | None = None,
        operation_id: str | None = None,
    ) -> OperationStatusResult:
        """Where an action stands: its status and, once submitted, the operation and receipt."""
        with self.db.read() as conn:
            if operation_id is not None:
                operation = owned(
                    operations.find_operation(conn, operation_id), customer_id, _owner
                )
                if operation.logical_action_id is None:
                    raise not_found()
                logical_action_id = operation.logical_action_id
            action = owned(actions.find_current(conn, str(logical_action_id)), customer_id, _owner)
            operation = operations.find_by_key(
                conn, operations.idempotency_key(action.logical_action_id, action.revision)
            )
            receipt = operations.receipt_of(conn, operation.operation_id) if operation else None
            return OperationStatusResult(
                logical_action_id=action.logical_action_id,
                revision=action.revision,
                action_status=action.status,
                operation=operation_view(conn, operation) if operation else None,
                receipt=receipt_view(conn, operation, receipt) if operation and receipt else None,
            )
