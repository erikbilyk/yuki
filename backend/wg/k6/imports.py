"""Statement import and reconciliation (SPEC §7, §13.3, S2 §2, §10).

A statement is imported line by line under UNIQUE (account_id, source_txn_id): a line already
stored is counted as a duplicate and changes nothing, so importing the same statement again is
a no-op. Each import leaves one batch row with its counts. Lines are history, never ledger
postings, and their text fields are untrusted.

Reconciliation is per account: the opening balance of its first statement plus every imported
line must equal the closing balance of its last statement. A line missing from an import
shows as a difference, which makes the cash snapshot incomplete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import now_db


@dataclass(frozen=True)
class StatementLine:
    source_txn_id: str
    posted_on: date
    amount_fen: int
    counterparty: str
    description: str
    category_hint: str | None


@dataclass(frozen=True)
class Statement:
    statement_id: str
    account_id: str
    period: str
    opening_fen: int
    closing_fen: int
    lines: tuple[StatementLine, ...]


@dataclass(frozen=True)
class ImportOutcome:
    batch_id: int
    new_count: int
    duplicate_count: int


@dataclass(frozen=True)
class Reconciliation:
    account_id: str
    opening_fen: int
    imported_sum_fen: int
    closing_fen: int

    @property
    def reconciled(self) -> bool:
        return self.opening_fen + self.imported_sum_fen == self.closing_fen

    @property
    def difference_fen(self) -> int:
        return self.closing_fen - self.opening_fen - self.imported_sum_fen


def import_statement(uow: UnitOfWork, statement: Statement) -> ImportOutcome:
    """Store the lines not seen before and record the batch; repeated lines are counted only."""
    stamp = now_db(uow)
    cursor = uow.execute(
        "INSERT INTO import_batches (account_id, statement_id, period, opening_fen, closing_fen,"
        " new_count, duplicate_count, created_at) VALUES (?, ?, ?, ?, ?, 0, 0, ?)",
        (
            statement.account_id,
            statement.statement_id,
            statement.period,
            statement.opening_fen,
            statement.closing_fen,
            stamp,
        ),
    )
    batch_id = int(cursor.lastrowid or 0)
    new_count = 0
    for line in statement.lines:
        inserted = uow.execute(
            "INSERT INTO transactions (account_id, source_txn_id, batch_id, posted_on, amount_fen,"
            " counterparty_raw, description_raw, category_hint, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (account_id, source_txn_id) DO NOTHING",
            (
                statement.account_id,
                line.source_txn_id,
                batch_id,
                line.posted_on.isoformat(),
                line.amount_fen,
                line.counterparty,
                line.description,
                line.category_hint,
                stamp,
            ),
        )
        new_count += inserted.rowcount
    duplicate_count = len(statement.lines) - new_count
    uow.execute(
        "UPDATE import_batches SET new_count = ?, duplicate_count = ? WHERE batch_id = ?",
        (new_count, duplicate_count, batch_id),
    )
    return ImportOutcome(batch_id, new_count, duplicate_count)


def reconciliation(reader: Reader, account_id: str) -> Reconciliation:
    """First opening plus all imported lines against the last closing; zeros without statements.

    Each statement counts once, by its first batch, so a repeated import does not change it.
    """
    chain = reader.execute(
        "SELECT opening_fen, closing_fen FROM import_batches WHERE batch_id IN"
        " (SELECT MIN(batch_id) FROM import_batches WHERE account_id = ? GROUP BY statement_id)"
        " ORDER BY period, batch_id",
        (account_id,),
    ).fetchall()
    imported = reader.execute(
        "SELECT COALESCE(SUM(amount_fen), 0) FROM transactions WHERE account_id = ?",
        (account_id,),
    ).fetchone()[0]
    return Reconciliation(
        account_id=account_id,
        opening_fen=chain[0]["opening_fen"] if chain else 0,
        imported_sum_fen=int(imported),
        closing_fen=chain[-1]["closing_fen"] if chain else 0,
    )


@dataclass(frozen=True)
class Line:
    account_id: str
    source_txn_id: str
    posted_on: date
    amount_fen: int
    counterparty_raw: str
    description_raw: str
    category: str | None
    resolution: str | None

    @property
    def resolved(self) -> bool:
        return self.category is not None or self.resolution is not None


_LINE_COLUMNS = (
    "t.account_id, t.source_txn_id, t.posted_on, t.amount_fen, t.counterparty_raw,"
    " t.description_raw, COALESCE(t.category_id, t.category_hint) AS category, t.resolution"
)


def _line(row) -> Line:
    return Line(
        account_id=row["account_id"],
        source_txn_id=row["source_txn_id"],
        posted_on=date.fromisoformat(row["posted_on"]),
        amount_fen=row["amount_fen"],
        counterparty_raw=row["counterparty_raw"],
        description_raw=row["description_raw"],
        category=row["category"],
        resolution=row["resolution"],
    )


def find_line(reader: Reader, customer_id: str, source_txn_id: str) -> Line | None:
    """A statement line of the customer's accounts by its source ID."""
    row = reader.execute(
        f"SELECT {_LINE_COLUMNS} FROM transactions t JOIN accounts a ON a.account_id = t.account_id"
        " WHERE a.customer_id = ? AND t.source_txn_id = ? ORDER BY t.posted_on LIMIT 1",
        (customer_id, source_txn_id),
    ).fetchone()
    return None if row is None else _line(row)


def customer_lines(
    reader: Reader,
    customer_id: str,
    *,
    since: date,
    account_id: str | None = None,
    category: str | None = None,
    unresolved_only: bool = False,
    limit: int | None = None,
) -> list[Line]:
    """The customer's lines posted on or after `since`, newest first."""
    sql = (
        f"SELECT {_LINE_COLUMNS} FROM transactions t JOIN accounts a ON a.account_id = t.account_id"
        " WHERE a.customer_id = ? AND t.posted_on >= ?"
    )
    params: list[object] = [customer_id, since.isoformat()]
    if account_id is not None:
        sql += " AND t.account_id = ?"
        params.append(account_id)
    if category is not None:
        sql += " AND COALESCE(t.category_id, t.category_hint) = ?"
        params.append(category)
    if unresolved_only:
        sql += " AND t.category_id IS NULL AND t.category_hint IS NULL AND t.resolution IS NULL"
    sql += " ORDER BY t.posted_on DESC, t.source_txn_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_line(row) for row in reader.execute(sql, params).fetchall()]


def resolve_line(uow: UnitOfWork, account_id: str, source_txn_id: str, category: str) -> None:
    """Set the class of a line the customer resolved; only the customer's action calls this."""
    cursor = uow.execute(
        "UPDATE transactions SET category_id = ?, resolution = 'customer', version = version + 1"
        " WHERE account_id = ? AND source_txn_id = ?",
        (category, account_id, source_txn_id),
    )
    if cursor.rowcount != 1:
        raise LookupError(f"no line {source_txn_id} on {account_id}")
