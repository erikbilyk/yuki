"""Cash engine inputs read from the current rows (SPEC §13.1, §13.2).

The gateway builds them inside its unit of work to recompute headroom before a
discretionary debit (SPEC §12.3 step 4); snapshots are built from the same inputs.

Monthly figures come from imported statement lines of the current Asia/Shanghai month on
covered accounts. A line's category is its resolved category, else the statement's hint.
Spending is outgoing lines except transfers between the customer's own accounts, refunds
and payments of purposes excluded from the allowance (matched through the commitment kind);
income is incoming lines except own transfers and refunds. Lines with no category are
unresolved.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from wg.contracts.engines import (
    CashAccountInput,
    CashInputs,
    CommitmentInput,
    HoldInput,
    UnresolvedInput,
)
from wg.contracts.enums import CommitmentStatus
from wg.k6 import accounts, imports, operations
from wg.k6.categories import NOT_INCOME, NOT_SPENDING
from wg.k6.db import Reader
from wg.k6.reserves import reserve_target
from wg.k6.simtime import SHANGHAI, now

RULES_VERSION = "rules_v1"
HORIZON_DAYS = 30


def cash_inputs_from_db(reader: Reader, customer_id: str, at: datetime | None = None) -> CashInputs:
    """The customer's cash inputs at `at` (default: the simulated now) from current rows."""
    at = now(reader) if at is None else at
    month_start = at.astimezone(SHANGHAI).date().replace(day=1)
    commitments = _commitments(reader, customer_id)
    lines = _month_lines(reader, customer_id, month_start)
    excluded = _excluded_categories(reader, customer_id, at, commitments)
    spent = sum(
        -amount for amount, category in lines if amount < 0 and _spending(category, excluded)
    )
    income = sum(amount for amount, category in lines if amount > 0 and category not in NOT_INCOME)
    return CashInputs(
        as_of=at,
        rules_version=RULES_VERSION,
        accounts=tuple(_account_inputs(reader, customer_id)),
        reserve_fen=reserve_target(reader, customer_id),
        allowance_remaining_fen=max(0, _allowance_budget(reader, customer_id, at) - spent),
        margin_fen=_margin(reader, customer_id),
        commitments=commitments,
        holds=tuple(
            HoldInput(operation_id=r.operation_id, purpose_id=r.purpose_id, amount_fen=r.amount_fen)
            for r in operations.active_reservations(reader, customer_id)
        ),
        unresolved=tuple(_unresolved(reader, customer_id)),
        income_this_month_fen=income,
        spent_this_month_fen=spent,
        horizon_days=HORIZON_DAYS,
    )


def _spending(category: str | None, excluded: frozenset[str]) -> bool:
    return category is not None and category not in NOT_SPENDING and category not in excluded


def _account_inputs(reader: Reader, customer_id: str) -> list[CashAccountInput]:
    """Each account carries its reconciliation figures (see `imports.reconciliation`)."""
    result = []
    for account in accounts.customer_accounts(reader, customer_id):
        rec = imports.reconciliation(reader, account.account_id)
        result.append(
            CashAccountInput(
                account_id=account.account_id,
                posted_fen=account.balance_fen,
                covered=account.covered,
                opening_fen=rec.opening_fen,
                imported_sum_fen=rec.imported_sum_fen,
                closing_fen=rec.closing_fen,
            )
        )
    return result


def _commitments(reader: Reader, customer_id: str) -> tuple[CommitmentInput, ...]:
    rows = reader.execute(
        "SELECT * FROM commitments WHERE customer_id = ? ORDER BY due_date, commitment_id",
        (customer_id,),
    ).fetchall()
    return tuple(
        CommitmentInput(
            commitment_id=row["commitment_id"],
            purpose_id=row["purpose_id"],
            status=CommitmentStatus(row["status"]),
            amount_fen=row["amount_fen"],
            unpaid_fen=row["unpaid_fen"],
            due_date=date.fromisoformat(row["due_date"]),
            payee_id=row["payee_id"],
        )
        for row in rows
    )


def _month_lines(
    reader: Reader, customer_id: str, month_start: date
) -> list[tuple[int, str | None]]:
    """(amount, category) of this month's statement lines on covered accounts."""
    rows = reader.execute(
        "SELECT t.amount_fen, COALESCE(t.category_id, t.category_hint) AS category"
        " FROM transactions t JOIN accounts a ON a.account_id = t.account_id"
        " WHERE a.customer_id = ? AND a.covered = 1 AND t.posted_on >= ?",
        (customer_id, month_start.isoformat()),
    ).fetchall()
    return [(row["amount_fen"], row["category"]) for row in rows]


def _excluded_categories(
    reader: Reader, customer_id: str, at: datetime, commitments: tuple[CommitmentInput, ...]
) -> frozenset[str]:
    """Categories of the purposes the allowance excludes, through their commitments' kinds."""
    row = _allowance(reader, customer_id, at)
    if row is None:
        return frozenset()
    excluded = frozenset(str(p) for p in json.loads(row["excluded_purpose_ids"]))
    purposes = {c.purpose_id for c in commitments if c.purpose_id in excluded}
    if not purposes:
        return frozenset()
    marks = ", ".join("?" for _ in purposes)
    kinds = reader.execute(
        f"SELECT DISTINCT kind FROM commitments WHERE customer_id = ? AND purpose_id IN ({marks})",
        (customer_id, *sorted(purposes)),
    ).fetchall()
    return frozenset(kind_row["kind"] for kind_row in kinds)


def _allowance(reader: Reader, customer_id: str, at: datetime):
    period = at.astimezone(SHANGHAI).strftime("%Y-%m")
    return reader.execute(
        "SELECT * FROM allowances WHERE customer_id = ? AND period = ?", (customer_id, period)
    ).fetchone()


def _allowance_budget(reader: Reader, customer_id: str, at: datetime) -> int:
    row = _allowance(reader, customer_id, at)
    return 0 if row is None else int(row["budget_fen"])


def _margin(reader: Reader, customer_id: str) -> int:
    row = reader.execute(
        "SELECT margin_fen FROM customer_settings WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    return 0 if row is None else int(row["margin_fen"])


def _unresolved(reader: Reader, customer_id: str) -> list[UnresolvedInput]:
    rows = reader.execute(
        "SELECT t.source_txn_id, t.amount_fen FROM transactions t"
        " JOIN accounts a ON a.account_id = t.account_id"
        " WHERE a.customer_id = ? AND t.category_id IS NULL AND t.category_hint IS NULL"
        " AND t.resolution IS NULL ORDER BY t.posted_on, t.source_txn_id",
        (customer_id,),
    ).fetchall()
    return [
        UnresolvedInput(txn_id=row["source_txn_id"], amount_fen=row["amount_fen"]) for row in rows
    ]
