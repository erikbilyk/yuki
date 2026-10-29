"""Reads of S2: the cash snapshot, bills, spending, transactions and unresolved lines.

Every figure comes from a stored snapshot or from statement rows. A read returns the
customer's latest snapshot when nothing changed since it was built, and builds a new one
otherwise (SPEC §13.3); the build is one unit of work and writes nothing else.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from wg.contracts.api import (
    NODE_PHRASE,
    AccountAvailability,
    BillsQuery,
    BillsView,
    BillView,
    CashDetails,
    CashFields,
    CashMode,
    CashSnapshotView,
    NextOperationView,
    SpendingGroupBy,
    SpendingQuery,
    SpendingRow,
    SpendingView,
    TransactionsQuery,
    TransactionsView,
    TransactionView,
    UnresolvedItem,
    UnresolvedView,
)
from wg.contracts.enums import ActionKind, BillStatus
from wg.core.k1_session import owned
from wg.k6 import accounts, actions, commitments, imports, operations, snapshots
from wg.k6.categories import NOT_SPENDING
from wg.k6.db import Database, Reader
from wg.k6.simtime import SHANGHAI, now
from wg.k6.snapshots import SnapshotRow
from wg.packs.bills import bill_sources
from wg.services.views import payee_display

UNRESOLVED_ROW = "unresolved"
_EARLIEST = date(1970, 1, 1)
_OWED_STATUSES = (BillStatus.SECURED, BillStatus.UNSECURED, BillStatus.OVERDUE)
_NEXT_STATUSES = frozenset(
    {"AWAITING_APPROVAL", "AWAITING_STEPUP", "READY", "SUBMITTED", "UNKNOWN", "PAUSED"}
)


class CashService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def current_snapshot(self, customer_id: str) -> SnapshotRow:
        """The latest snapshot if it matches the current rows and time, else a new one."""
        with self.db.read() as conn:
            if snapshots.is_current(conn, customer_id):
                latest = snapshots.latest_snapshot(conn, customer_id)
                assert latest is not None
                return latest
        return self.db.run_in_uow(
            lambda uow: snapshots.ensure_snapshot(uow, customer_id), "cash snapshot"
        )

    def snapshot(self, customer_id: str) -> CashSnapshotView:
        snapshot = self.current_snapshot(customer_id)
        values = snapshot.values
        owed = [b for b in values.bills if b.status in _OWED_STATUSES]
        with self.db.read() as conn:
            availability = tuple(_availability(conn, customer_id, values.quality.reconciled))
            next_operation = _next_operation(conn, customer_id)
        shortfall = values.shortfall_fen
        return CashSnapshotView(
            snapshot_id=snapshot.snapshot_id,
            as_of=snapshot.as_of,
            mode=CashMode.SHORTFALL if shortfall > 0 else CashMode.HEADROOM,
            headroom_fen=values.headroom_fen,
            shortfall_fen=shortfall,
            protected_fen=values.protected_fen,
            next_operation=next_operation,
            unpaid_bills_fen=sum(b.unpaid_fen for b in owed),
            unpaid_bills_count=len(owed),
            details=CashDetails(
                rules_version=snapshot.rules_version,
                c_fen=values.c_fen,
                e_fen=values.e_fen,
                l_fen=values.l_fen,
                b_fen=values.b_fen,
                m_fen=values.m_fen,
                u_fen=values.u_fen,
                accounts=availability,
                monthly_estimate_fen=values.monthly_estimate_fen,
                bills=values.bills,
                quality=values.quality,
            ),
        )

    def cash_fields(self, customer_id: str) -> CashFields:
        """The four persistent fields alone, from the current snapshot."""
        view = self.snapshot(customer_id)
        return CashFields.model_validate(view.model_dump(exclude={"details"}))

    def bills(self, customer_id: str, query: BillsQuery) -> BillsView:
        snapshot = self.current_snapshot(customer_id)
        derived = {b.commitment_id: b.status for b in snapshot.values.bills}
        with self.db.read() as conn:
            rows = commitments.customer_commitments(conn, customer_id, query.status)
            if query.horizon_days is not None:
                last = now(conn).astimezone(SHANGHAI).date() + timedelta(days=query.horizon_days)
                rows = [row for row in rows if row.due_date <= last]
            views = tuple(
                BillView(
                    commitment_id=row.commitment_id,
                    purpose_id=row.purpose_id,
                    kind=row.kind,
                    payee_id=row.payee_id,
                    payee_display=payee_display(conn, row.payee_id) or row.payee_id,
                    amount_fen=row.amount_fen,
                    unpaid_fen=row.unpaid_fen,
                    due_date=row.due_date,
                    recurrence=row.recurrence,
                    status=row.status,
                    derived=derived.get(row.commitment_id),
                    source=row.source,
                    sources=bill_sources(row),
                )
                for row in rows
            )
        return BillsView(snapshot_id=snapshot.snapshot_id, bills=views)

    def spending(self, customer_id: str, query: SpendingQuery) -> SpendingView:
        """Outgoing lines by category or month; lines nothing has classified are their own row."""
        with self.db.read() as conn:
            today = now(conn).astimezone(SHANGHAI).date()
            lines = imports.customer_lines(
                conn, customer_id, since=today - timedelta(days=query.days)
            )
        totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for line in lines:
            if line.amount_fen >= 0 or line.category in NOT_SPENDING:
                continue
            if query.group_by is SpendingGroupBy.MONTH:
                key = line.posted_on.strftime("%Y-%m")
            else:
                key = line.category or UNRESOLVED_ROW
            totals[key][0] += -line.amount_fen
            totals[key][1] += 1
        rows = tuple(
            SpendingRow(key=key, spent_fen=spent, count=count)
            for key, (spent, count) in sorted(
                totals.items(), key=lambda item: (-item[1][0], item[0])
            )
        )
        return SpendingView(
            from_date=today - timedelta(days=query.days),
            to_date=today,
            group_by=query.group_by,
            rows=rows,
            total_spent_fen=sum(row.spent_fen for row in rows),
        )

    def transactions(self, customer_id: str, query: TransactionsQuery) -> TransactionsView:
        with self.db.read() as conn:
            if query.account_id is not None:
                owned(accounts.find_account(conn, query.account_id), customer_id, _customer)
            today = now(conn).astimezone(SHANGHAI).date()
            lines = imports.customer_lines(
                conn,
                customer_id,
                since=today - timedelta(days=query.days),
                account_id=query.account_id,
                category=query.category,
                limit=query.limit,
            )
        return TransactionsView(
            transactions=tuple(
                TransactionView(
                    txn_id=line.source_txn_id,
                    account_id=line.account_id,
                    posted_on=line.posted_on,
                    amount_fen=line.amount_fen,
                    counterparty_raw=line.counterparty_raw,
                    description_raw=line.description_raw,
                    category=line.category,
                    resolved=line.resolved,
                )
                for line in lines
            )
        )

    def unresolved(self, customer_id: str) -> UnresolvedView:
        snapshot = self.current_snapshot(customer_id)
        with self.db.read() as conn:
            lines = imports.customer_lines(conn, customer_id, since=_EARLIEST, unresolved_only=True)
        items = tuple(
            UnresolvedItem(
                txn_id=line.source_txn_id,
                account_id=line.account_id,
                posted_on=line.posted_on,
                amount_fen=line.amount_fen,
                description_raw=line.description_raw,
                impact_fen=abs(line.amount_fen),
            )
            for line in lines
        )
        return UnresolvedView(
            snapshot_id=snapshot.snapshot_id,
            items=items,
            count=len(items),
            impact_fen=sum(item.impact_fen for item in items),
        )


def _customer(account: accounts.AccountRow) -> str | None:
    return account.customer_id


def _availability(
    reader: Reader, customer_id: str, reconciled: dict[str, bool]
) -> list[AccountAvailability]:
    return [
        AccountAvailability(
            account_id=account.account_id,
            name=account.name,
            covered=account.covered,
            posted_fen=account.balance_fen,
            available_fen=account.balance_fen - operations.reserved_fen(reader, account.account_id),
            reconciled=reconciled.get(account.account_id) if account.covered else None,
        )
        for account in accounts.customer_accounts(reader, customer_id)
    ]


def _next_operation(reader: Reader, customer_id: str) -> NextOperationView | None:
    """The customer's next pending step: the open action due soonest."""
    action = actions.next_open_action(reader, customer_id, _NEXT_STATUSES)
    if action is None:
        return None
    amount = action.params.get("amount_fen")
    return NextOperationView(
        logical_action_id=action.logical_action_id,
        plan_id=action.plan_id,
        kind=ActionKind(action.kind),
        status=action.status,
        phrase=NODE_PHRASE[action.status],
        amount_fen=amount if isinstance(amount, int) else None,
        not_before=action.not_before,
    )
