"""Append-only audit log (SPEC §7, §16). Rows describe decisions, never model reasoning."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from wg.contracts.enums import AuditActor
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, now_db


@dataclass(frozen=True)
class AuditRow:
    audit_id: int
    at: datetime
    actor: AuditActor
    event: str
    customer_id: str | None
    ref: str | None
    decision: str | None
    reason: str | None


def record(
    uow: UnitOfWork,
    actor: AuditActor,
    event: str,
    *,
    customer_id: str | None = None,
    ref: str | None = None,
    decision: str | None = None,
    reason: str | None = None,
) -> int:
    """Append one audit row at the current simulated time and return its id."""
    cursor = uow.execute(
        "INSERT INTO audit_log (at, actor, event, customer_id, ref, decision, reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now_db(uow), AuditActor(actor).value, event, customer_id, ref, decision, reason),
    )
    return int(cursor.lastrowid or 0)


def list_audit(
    reader: Reader,
    *,
    customer_id: str | None = None,
    refs: Collection[str] | None = None,
    limit: int = 200,
) -> list[AuditRow]:
    """Newest first; with `customer_id`, only that customer's rows; with `refs`, only those refs."""
    sql = "SELECT * FROM audit_log WHERE 1 = 1"
    params: list[object] = []
    if customer_id is not None:
        sql += " AND customer_id = ?"
        params.append(customer_id)
    if refs is not None:
        sql += f" AND ref IN ({', '.join('?' for _ in refs) or 'NULL'})"
        params.extend(refs)
    rows = reader.execute(sql + " ORDER BY audit_id DESC LIMIT ?", (*params, limit)).fetchall()
    return [
        AuditRow(
            audit_id=row["audit_id"],
            at=from_db(row["at"]),
            actor=AuditActor(row["actor"]),
            event=row["event"],
            customer_id=row["customer_id"],
            ref=row["ref"],
            decision=row["decision"],
            reason=row["reason"],
        )
        for row in rows
    ]
