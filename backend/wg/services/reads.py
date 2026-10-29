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
    PlanSummary,
    PlansView,
    ReceiptView,
    TransferHistoryView,
)
from wg.contracts.enums import PlanKind, PlanMode, PlanStatus
from wg.core.k1_session import not_found, owned
from wg.core.k3_policy import DAILY_SUM_KINDS
from wg.k6 import accounts, operations
from wg.k6.db import Database
from wg.k6.simtime import from_db, now
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

    def plans(self, customer_id: str) -> PlansView:
        """The customer's plans as stored; their nodes and expiry are read per plan.

        A standalone action's own plan (kind `single`) is not listed; the action is.
        """
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT plan_id, kind, status, mode, created_at FROM plans"
                " WHERE customer_id = ? AND kind <> ? ORDER BY created_at, plan_id",
                (customer_id, PlanKind.SINGLE.value),
            ).fetchall()
        return PlansView(
            plans=tuple(
                PlanSummary(
                    plan_id=row["plan_id"],
                    kind=PlanKind(row["kind"]),
                    status=PlanStatus(row["status"]),
                    mode=PlanMode(row["mode"]),
                    created_at=from_db(row["created_at"]),
                )
                for row in rows
            )
        )
