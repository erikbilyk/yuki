"""Fault injection that no route offers: the one place a test writes to the database.

T21 needs a commitment whose unpaid amount disagrees with its payments, which only a
defect or outside tampering can produce. The write uses its own connection and one
statement; the product must detect the result and refuse further writes.
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
