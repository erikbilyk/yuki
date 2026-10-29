"""Fault injection that no route offers: the only place a test writes to the database.

T21 needs a commitment whose unpaid amount disagrees with its payments, which only a
defect or outside tampering can produce. T58 needs an imported transaction to disappear
after a plan started, and T56 needs a domain event to be delivered a second time. R4
needs the bank's record of an order to vanish, which reconciliation must treat as an
integrity violation. Each write uses its own connection and one statement; the product
must cope with the result.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def corrupt_unpaid(db_path: Path, commitment_id: str, unpaid_fen: int) -> None:
    """Set `commitments.unpaid_fen` directly, bypassing the gateway and its version check."""
    with closing(sqlite3.connect(db_path, timeout=10.0, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            "UPDATE commitments SET unpaid_fen = ? WHERE commitment_id = ?",
            (unpaid_fen, commitment_id),
        ).rowcount
        if updated != 1:
            conn.execute("ROLLBACK")
            raise AssertionError(f"no commitment {commitment_id}")
        conn.execute("COMMIT")


def drop_imported_transaction(db_path: Path, source_txn_id: str) -> None:
    """Delete one imported statement line, so that its account no longer reconciles."""
    with closing(sqlite3.connect(db_path, timeout=10.0, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        deleted = conn.execute(
            "DELETE FROM transactions WHERE source_txn_id = ?", (source_txn_id,)
        ).rowcount
        if deleted != 1:
            conn.execute("ROLLBACK")
            raise AssertionError(f"{deleted} transactions with source id {source_txn_id}")
        conn.execute("COMMIT")


def rewind_event_cursors(db_path: Path) -> None:
    """Make every event consumer start again from the first event at its next start."""
    with closing(sqlite3.connect(db_path, timeout=10.0, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE event_cursors SET last_event_id = ''")
        conn.execute("COMMIT")


def delete_bank_request(db_path: Path, idempotency_key: str) -> None:
    """Remove the simulated bank's record of one order, leaving the operation in place."""
    with closing(sqlite3.connect(db_path, timeout=10.0, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        deleted = conn.execute(
            "DELETE FROM bank_requests WHERE idempotency_key = ?", (idempotency_key,)
        ).rowcount
        if deleted != 1:
            conn.execute("ROLLBACK")
            raise AssertionError(f"{deleted} bank requests with key {idempotency_key}")
        conn.execute("COMMIT")
