"""The one write the runner makes to an episode database: product settlement periods.

No route changes a product's settlement period, and a variant applies only at reset. A scenario
that slows the redemption down after a plan was approved therefore edits the rows itself, in a
single `BEGIN IMMEDIATE` transaction on its own connection while the backend runs.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

BUSY_TIMEOUT_S = 10.0


def set_settle_days(db_path: Path, *, redemption: int | None, purchase: int | None) -> int:
    """Set the given periods on every product; returns the number of products changed."""
    assignments = {
        column: days
        for column, days in (
            ("redeem_settle_days", redemption),
            ("purchase_settle_days", purchase),
        )
        if days is not None
    }
    if not assignments:
        raise ValueError("no settlement period given")
    columns = ", ".join(f"{column} = ?" for column in assignments)
    with closing(sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_S, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = conn.execute(
                f"UPDATE products SET {columns}",
                tuple(assignments.values()),
            ).rowcount
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    return changed
