"""The customer's reserve for the unexpected: the target E of the cash formula (SPEC §13.1)."""

from __future__ import annotations

from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import now_db

RESERVE_KIND = "emergency"


def reserve_target(reader: Reader, customer_id: str) -> int:
    row = reader.execute(
        "SELECT target_fen FROM reserves WHERE customer_id = ? AND kind = ?",
        (customer_id, RESERVE_KIND),
    ).fetchone()
    return 0 if row is None else int(row["target_fen"])


def set_reserve_target(uow: UnitOfWork, customer_id: str, target_fen: int) -> None:
    """Set the target; the row's version rises with every change."""
    uow.execute(
        "INSERT INTO reserves (customer_id, kind, target_fen, created_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (customer_id, kind) DO UPDATE SET"
        " target_fen = excluded.target_fen, version = version + 1",
        (customer_id, RESERVE_KIND, target_fen, now_db(uow)),
    )
