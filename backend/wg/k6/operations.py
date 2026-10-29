"""Operations, their events, reservations and receipts (SPEC §7, §12).

An operation is created once per idempotency key. Its status moves only along
OPERATION_TRANSITIONS; a terminal status is never overwritten. A reservation is created
with its operation and leaves ACTIVE once; a receipt is written once, at the terminal
outcome.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from wg.contracts.enums import (
    ActionKind,
    OperationEventKind,
    OperationStatus,
    ReceiptStatus,
    ReservationState,
)
from wg.contracts.ids import IdPrefix, new_id
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, now_db, to_db

_O = OperationStatus

OPERATION_TRANSITIONS: Mapping[OperationStatus, frozenset[OperationStatus]] = {
    _O.SUBMITTED: frozenset({_O.SETTLED, _O.REJECTED, _O.UNKNOWN}),
    _O.UNKNOWN: frozenset({_O.SUBMITTED, _O.SETTLED, _O.REJECTED}),
    _O.SETTLED: frozenset(),
    _O.REJECTED: frozenset(),
}


class StaleOperation(RuntimeError):
    """A status change that is not allowed, or a row that changed since it was read."""


def idempotency_key(logical_action_id: str, revision: int) -> str:
    """One key per logical action and revision (SPEC §12.2)."""
    return f"{logical_action_id}:{revision}"


@dataclass(frozen=True)
class OperationRow:
    operation_id: str
    logical_action_id: str | None
    revision: int | None
    idempotency_key: str
    request_hash: str
    kind: str
    customer_id: str | None
    account_id: str | None
    amount_fen: int | None
    shares: Decimal | None
    product_id: str | None
    payee_id: str | None
    commitment_id: str | None
    status: OperationStatus
    submitted_at: datetime
    settle_due_at: datetime | None
    settled_at: datetime | None
    version: int

    @property
    def action_kind(self) -> ActionKind:
        return ActionKind(self.kind)


def _opt_time(text: str | None) -> datetime | None:
    return from_db(text) if text else None


def _operation(row) -> OperationRow:
    return OperationRow(
        operation_id=row["operation_id"],
        logical_action_id=row["logical_action_id"],
        revision=row["revision"],
        idempotency_key=row["idempotency_key"],
        request_hash=row["request_hash"],
        kind=row["kind"],
        customer_id=row["customer_id"],
        account_id=row["account_id"],
        amount_fen=row["amount_fen"],
        shares=Decimal(row["shares"]) if row["shares"] is not None else None,
        product_id=row["product_id"],
        payee_id=row["payee_id"],
        commitment_id=row["commitment_id"],
        status=OperationStatus(row["status"]),
        submitted_at=from_db(row["submitted_at"]),
        settle_due_at=_opt_time(row["settle_due_at"]),
        settled_at=_opt_time(row["settled_at"]),
        version=row["version"],
    )


def insert_operation(
    uow: UnitOfWork,
    *,
    key: str,
    request_hash: str,
    kind: str,
    submitted_at: datetime,
    status: OperationStatus = OperationStatus.SUBMITTED,
    logical_action_id: str | None = None,
    revision: int | None = None,
    customer_id: str | None = None,
    account_id: str | None = None,
    amount_fen: int | None = None,
    shares: Decimal | None = None,
    product_id: str | None = None,
    payee_id: str | None = None,
    commitment_id: str | None = None,
    settle_due_at: datetime | None = None,
    settled_at: datetime | None = None,
) -> OperationRow:
    """Insert the one operation of `key`; a second insert with the same key fails."""
    operation_id = new_id(IdPrefix.OPERATION)
    uow.execute(
        "INSERT INTO operations (operation_id, logical_action_id, revision, idempotency_key,"
        " request_hash, kind, customer_id, account_id, amount_fen, shares, product_id,"
        " payee_id, commitment_id, status, submitted_at, settle_due_at, settled_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            operation_id,
            logical_action_id,
            revision,
            key,
            request_hash,
            kind,
            customer_id,
            account_id,
            amount_fen,
            str(shares) if shares is not None else None,
            product_id,
            payee_id,
            commitment_id,
            OperationStatus(status).value,
            to_db(submitted_at),
            to_db(settle_due_at) if settle_due_at else None,
            to_db(settled_at) if settled_at else None,
            now_db(uow),
        ),
    )
    return get_operation(uow, operation_id)


def find_operation(reader: Reader, operation_id: str) -> OperationRow | None:
    row = reader.execute(
        "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    return None if row is None else _operation(row)


def get_operation(reader: Reader, operation_id: str) -> OperationRow:
    operation = find_operation(reader, operation_id)
    if operation is None:
        raise LookupError(f"no operation {operation_id}")
    return operation


def find_by_key(reader: Reader, key: str) -> OperationRow | None:
    row = reader.execute("SELECT * FROM operations WHERE idempotency_key = ?", (key,)).fetchone()
    return None if row is None else _operation(row)


def set_status(
    uow: UnitOfWork,
    operation: OperationRow,
    to: OperationStatus,
    *,
    settled_at: datetime | None = None,
) -> OperationRow:
    """Move an operation to `to`; the row must still be at `operation.version`."""
    if to not in OPERATION_TRANSITIONS[operation.status]:
        raise StaleOperation(f"{operation.operation_id}: {operation.status} -> {to}")
    cursor = uow.execute(
        "UPDATE operations SET status = ?, settled_at = COALESCE(?, settled_at),"
        " version = version + 1 WHERE operation_id = ? AND version = ?",
        (
            to.value,
            to_db(settled_at) if settled_at else None,
            operation.operation_id,
            operation.version,
        ),
    )
    if cursor.rowcount != 1:
        raise StaleOperation(f"{operation.operation_id} changed since version {operation.version}")
    return get_operation(uow, operation.operation_id)


def append_event(
    uow: UnitOfWork, operation_id: str, event: OperationEventKind, reason: str | None = None
) -> None:
    uow.execute(
        "INSERT INTO operation_events (operation_id, event, reason, at) VALUES (?, ?, ?, ?)",
        (operation_id, OperationEventKind(event).value, reason, now_db(uow)),
    )


def operation_events(reader: Reader, operation_id: str) -> list[OperationEventKind]:
    rows = reader.execute(
        "SELECT event FROM operation_events WHERE operation_id = ? ORDER BY operation_event_id",
        (operation_id,),
    ).fetchall()
    return [OperationEventKind(row["event"]) for row in rows]


def due_operations(reader: Reader, at: datetime) -> list[OperationRow]:
    """SUBMITTED and UNKNOWN customer operations whose settlement is due at or before `at`."""
    rows = reader.execute(
        "SELECT * FROM operations WHERE status IN (?, ?) AND logical_action_id IS NOT NULL"
        " AND settle_due_at IS NOT NULL AND settle_due_at <= ?"
        " ORDER BY settle_due_at, operation_id",
        (_O.SUBMITTED.value, _O.UNKNOWN.value, to_db(at)),
    ).fetchall()
    return [_operation(row) for row in rows]


def open_operations(
    reader: Reader, customer_id: str | None = None, plan_id: str | None = None
) -> list[OperationRow]:
    """Customer operations whose outcome is not final: SUBMITTED and UNKNOWN."""
    sql = (
        "SELECT o.* FROM operations o LEFT JOIN actions a"
        " ON a.logical_action_id = o.logical_action_id AND a.revision = o.revision"
        " WHERE o.status IN (?, ?) AND o.logical_action_id IS NOT NULL"
    )
    params: list[Any] = [_O.SUBMITTED.value, _O.UNKNOWN.value]
    if customer_id is not None:
        sql += " AND o.customer_id = ?"
        params.append(customer_id)
    if plan_id is not None:
        sql += " AND a.plan_id = ?"
        params.append(plan_id)
    rows = reader.execute(sql + " ORDER BY o.submitted_at, o.operation_id", params).fetchall()
    return [_operation(row) for row in rows]


def customer_operations(
    reader: Reader,
    customer_id: str,
    *,
    kinds: Sequence[ActionKind],
    since: datetime,
    payee_id: str | None = None,
) -> list[OperationRow]:
    """The customer's operations of `kinds` submitted at or after `since`, newest first."""
    marks = ", ".join("?" for _ in kinds)
    sql = (
        f"SELECT * FROM operations WHERE customer_id = ? AND kind IN ({marks})"
        " AND submitted_at >= ?"
    )
    params: list[Any] = [customer_id, *(ActionKind(k).value for k in kinds), to_db(since)]
    if payee_id is not None:
        sql += " AND payee_id = ?"
        params.append(payee_id)
    rows = reader.execute(sql + " ORDER BY submitted_at DESC, operation_id", params).fetchall()
    return [_operation(row) for row in rows]


# Reservations


@dataclass(frozen=True)
class ReservationRow:
    reservation_id: str
    operation_id: str
    purpose_id: str | None
    account_id: str
    amount_fen: int
    state: ReservationState
    version: int


def _reservation(row) -> ReservationRow:
    return ReservationRow(
        reservation_id=row["reservation_id"],
        operation_id=row["operation_id"],
        purpose_id=row["purpose_id"],
        account_id=row["account_id"],
        amount_fen=row["amount_fen"],
        state=ReservationState(row["state"]),
        version=row["version"],
    )


def insert_reservation(
    uow: UnitOfWork,
    *,
    operation_id: str,
    account_id: str,
    amount_fen: int,
    purpose_id: str | None = None,
) -> ReservationRow:
    """Hold `amount_fen` on the account for one operation; one reservation per operation."""
    reservation_id = new_id(IdPrefix.RESERVATION)
    uow.execute(
        "INSERT INTO reservations (reservation_id, operation_id, purpose_id, account_id,"
        " amount_fen, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            reservation_id,
            operation_id,
            purpose_id,
            account_id,
            amount_fen,
            ReservationState.ACTIVE.value,
            now_db(uow),
        ),
    )
    reservation = reservation_of(uow, operation_id)
    assert reservation is not None
    return reservation


def reservation_of(reader: Reader, operation_id: str) -> ReservationRow | None:
    row = reader.execute(
        "SELECT * FROM reservations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    return None if row is None else _reservation(row)


def close_reservation(
    uow: UnitOfWork, reservation: ReservationRow, state: ReservationState
) -> ReservationRow:
    """Consume or release an ACTIVE reservation; it never becomes ACTIVE again."""
    if state is ReservationState.ACTIVE or reservation.state is not ReservationState.ACTIVE:
        raise StaleOperation(f"{reservation.reservation_id}: {reservation.state} -> {state}")
    cursor = uow.execute(
        "UPDATE reservations SET state = ?, version = version + 1"
        " WHERE reservation_id = ? AND version = ?",
        (state.value, reservation.reservation_id, reservation.version),
    )
    if cursor.rowcount != 1:
        raise StaleOperation(f"{reservation.reservation_id} changed since it was read")
    closed = reservation_of(uow, reservation.operation_id)
    assert closed is not None
    return closed


def active_reservations(reader: Reader, customer_id: str) -> list[ReservationRow]:
    """ACTIVE reservations on the customer's accounts: the outgoing holds."""
    rows = reader.execute(
        "SELECT r.* FROM reservations r JOIN accounts a ON a.account_id = r.account_id"
        " WHERE a.customer_id = ? AND r.state = ? ORDER BY r.created_at, r.reservation_id",
        (customer_id, ReservationState.ACTIVE.value),
    ).fetchall()
    return [_reservation(row) for row in rows]


def reserved_fen(reader: Reader, account_id: str) -> int:
    row = reader.execute(
        "SELECT COALESCE(SUM(amount_fen), 0) FROM reservations WHERE account_id = ? AND state = ?",
        (account_id, ReservationState.ACTIVE.value),
    ).fetchone()
    return int(row[0])


# Receipts


@dataclass(frozen=True)
class ReceiptRow:
    receipt_id: str
    operation_id: str
    status: ReceiptStatus
    terms_hash: str | None
    effect: Mapping[str, Any]
    reason_code: str | None
    created_at: datetime


def _receipt(row) -> ReceiptRow:
    return ReceiptRow(
        receipt_id=row["receipt_id"],
        operation_id=row["operation_id"],
        status=ReceiptStatus(row["status"]),
        terms_hash=row["terms_hash"],
        effect=json.loads(row["effect_json"]),
        reason_code=row["reason_code"],
        created_at=from_db(row["created_at"]),
    )


def insert_receipt(
    uow: UnitOfWork,
    *,
    operation_id: str,
    status: ReceiptStatus,
    effect: Mapping[str, Any],
    terms_hash: str | None = None,
    reason_code: str | None = None,
) -> ReceiptRow:
    """The single terminal receipt of an operation; a second one fails on its UNIQUE key."""
    receipt_id = new_id(IdPrefix.RECEIPT)
    uow.execute(
        "INSERT INTO receipts (receipt_id, operation_id, status, terms_hash, effect_json,"
        " reason_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            receipt_id,
            operation_id,
            ReceiptStatus(status).value,
            terms_hash,
            json.dumps(dict(effect), sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            reason_code,
            now_db(uow),
        ),
    )
    receipt = receipt_of(uow, operation_id)
    assert receipt is not None
    return receipt


def receipt_of(reader: Reader, operation_id: str) -> ReceiptRow | None:
    row = reader.execute(
        "SELECT * FROM receipts WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    return None if row is None else _receipt(row)
