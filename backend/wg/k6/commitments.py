"""Commitments (bills) and their append-only event history (SPEC §7, §13.4, S2 §6).

A commitment moves along STATUS_TRANSITIONS. Every change is one version-checked update of
the row, one `commitment_events` row and, when it changes what the customer must keep
protected, one `commitments_changed` domain event, all in the caller's unit of work.

For CONFIRMED and PARTIALLY_PAID bills `unpaid_fen` equals `amount_fen` minus the settled
payments with that `commitment_id`. PAID_REPORTED and CANCELLED bills leave the protected
amount by status and keep `unpaid_fen`, so the rule does not apply to them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from wg.contracts.enums import (
    ActionKind,
    CommitmentStatus,
    OperationStatus,
    Recurrence,
)
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.events import CommitmentChange, CommitmentsChanged, DomainEventType
from wg.contracts.ids import IdPrefix, new_id
from wg.k6 import events
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import now_db

_S = CommitmentStatus

STATUS_TRANSITIONS: Mapping[CommitmentStatus, frozenset[CommitmentStatus]] = {
    _S.CANDIDATE: frozenset({_S.CONFIRMED, _S.DISMISSED}),
    _S.CONFIRMED: frozenset({_S.PARTIALLY_PAID, _S.PAID, _S.PAID_REPORTED, _S.CANCELLED}),
    _S.PARTIALLY_PAID: frozenset({_S.PARTIALLY_PAID, _S.PAID, _S.PAID_REPORTED, _S.CANCELLED}),
    _S.PAID: frozenset(),
    _S.PAID_REPORTED: frozenset(),
    _S.CANCELLED: frozenset(),
    _S.DISMISSED: frozenset(),
}

# Statuses of a bill that is still owed: they enter B and carry the unpaid invariant.
OWED = frozenset({_S.CONFIRMED, _S.PARTIALLY_PAID})

CANDIDATE_EVENT = "candidate"


class StaleCommitment(RuntimeError):
    """A change from a status the transitions do not allow, or to a row that changed since read."""


@dataclass(frozen=True)
class CommitmentRow:
    commitment_id: str
    customer_id: str
    purpose_id: str
    kind: str
    payee_id: str
    amount_fen: int
    unpaid_fen: int
    due_date: date
    recurrence: Recurrence
    source: str
    status: CommitmentStatus
    version: int


def _commitment(row) -> CommitmentRow:
    return CommitmentRow(
        commitment_id=row["commitment_id"],
        customer_id=row["customer_id"],
        purpose_id=row["purpose_id"],
        kind=row["kind"],
        payee_id=row["payee_id"],
        amount_fen=row["amount_fen"],
        unpaid_fen=row["unpaid_fen"],
        due_date=date.fromisoformat(row["due_date"]),
        recurrence=Recurrence(row["recurrence"]),
        source=row["source"],
        status=CommitmentStatus(row["status"]),
        version=row["version"],
    )


def find_commitment(reader: Reader, commitment_id: str) -> CommitmentRow | None:
    row = reader.execute(
        "SELECT * FROM commitments WHERE commitment_id = ?", (commitment_id,)
    ).fetchone()
    return None if row is None else _commitment(row)


def customer_commitments(
    reader: Reader, customer_id: str, status: CommitmentStatus | None = None
) -> list[CommitmentRow]:
    """The customer's commitments by due date, optionally of one status."""
    sql = "SELECT * FROM commitments WHERE customer_id = ?"
    params: list[object] = [customer_id]
    if status is not None:
        sql += " AND status = ?"
        params.append(CommitmentStatus(status).value)
    rows = reader.execute(sql + " ORDER BY due_date, commitment_id", params).fetchall()
    return [_commitment(row) for row in rows]


def find_duplicate_candidate(
    reader: Reader, customer_id: str, payee_id: str, kind: str, amount_fen: int, due_date: date
) -> CommitmentRow | None:
    """An open candidate or confirmed bill with the same payee, kind, amount and due date."""
    row = reader.execute(
        "SELECT * FROM commitments WHERE customer_id = ? AND payee_id = ? AND kind = ?"
        " AND amount_fen = ? AND due_date = ? AND status IN (?, ?, ?)",
        (
            customer_id,
            payee_id,
            kind,
            amount_fen,
            due_date.isoformat(),
            _S.CANDIDATE.value,
            _S.CONFIRMED.value,
            _S.PARTIALLY_PAID.value,
        ),
    ).fetchone()
    return None if row is None else _commitment(row)


def insert_candidate(
    uow: UnitOfWork,
    *,
    customer_id: str,
    payee_id: str,
    kind: str,
    amount_fen: int,
    due_date: date,
    source: str,
    recurrence: Recurrence = Recurrence.NONE,
) -> CommitmentRow:
    """A bill that exists but protects nothing until the customer confirms it."""
    commitment_id = new_id(IdPrefix.COMMITMENT)
    uow.execute(
        "INSERT INTO commitments (commitment_id, customer_id, purpose_id, kind, payee_id,"
        " amount_fen, unpaid_fen, due_date, recurrence, source, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            commitment_id,
            customer_id,
            new_id(IdPrefix.PURPOSE),
            kind,
            payee_id,
            amount_fen,
            amount_fen,
            due_date.isoformat(),
            recurrence.value,
            source,
            _S.CANDIDATE.value,
            now_db(uow),
        ),
    )
    row = get_commitment(uow, commitment_id)
    _append_event(uow, row, CANDIDATE_EVENT, 0, source)
    return row


def get_commitment(reader: Reader, commitment_id: str) -> CommitmentRow:
    row = find_commitment(reader, commitment_id)
    if row is None:
        raise LookupError(f"no commitment {commitment_id}")
    return row


def transition(
    uow: UnitOfWork,
    commitment: CommitmentRow,
    *,
    to: CommitmentStatus,
    change: CommitmentChange,
    delta_fen: int,
    unpaid_fen: int | None = None,
    source_ref: str | None = None,
) -> CommitmentRow:
    """Move a commitment to `to`; the row must still be at `commitment.version`.

    `delta_fen` is the change in the amount the bill adds to B: positive when it is confirmed,
    negative when it is paid, cancelled or reported paid. `unpaid_fen` defaults to the current one.
    """
    if to not in STATUS_TRANSITIONS[commitment.status]:
        raise StaleCommitment(f"{commitment.commitment_id}: {commitment.status} -> {to}")
    unpaid = commitment.unpaid_fen if unpaid_fen is None else unpaid_fen
    cursor = uow.execute(
        "UPDATE commitments SET status = ?, unpaid_fen = ?, version = version + 1"
        " WHERE commitment_id = ? AND version = ?",
        (CommitmentStatus(to).value, unpaid, commitment.commitment_id, commitment.version),
    )
    if cursor.rowcount != 1:
        raise StaleCommitment(
            f"{commitment.commitment_id} changed since version {commitment.version}"
        )
    row = get_commitment(uow, commitment.commitment_id)
    event_id = _append_event(uow, row, change.value, delta_fen, source_ref)
    events.append_event(
        uow,
        DomainEventType.COMMITMENTS_CHANGED,
        CommitmentsChanged(
            commitment_event_id=event_id,
            commitment_id=row.commitment_id,
            purpose_id=row.purpose_id,
            change=change,
            delta_fen=delta_fen,
            unpaid_fen=row.unpaid_fen,
            commitments_version=commitments_version(uow, row.customer_id),
        ),
        customer_id=row.customer_id,
    )
    return row


def _append_event(
    uow: UnitOfWork, row: CommitmentRow, change: str, delta_fen: int, source_ref: str | None
) -> int:
    cursor = uow.execute(
        "INSERT INTO commitment_events (commitment_id, change, delta_fen, unpaid_fen,"
        " commitment_version, source_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row.commitment_id,
            change,
            delta_fen,
            row.unpaid_fen,
            row.version,
            source_ref,
            now_db(uow),
        ),
    )
    return int(cursor.lastrowid or 0)


def commitments_version(reader: Reader, customer_id: str) -> int:
    """How many changes the customer's bills have had; a candidate changes nothing protected."""
    row = reader.execute(
        "SELECT COUNT(*) FROM commitment_events e JOIN commitments c"
        " ON c.commitment_id = e.commitment_id WHERE c.customer_id = ? AND e.change <> ?",
        (customer_id, CANDIDATE_EVENT),
    ).fetchone()
    return int(row[0])


def additions_after(reader: Reader, customer_id: str, version: int) -> int:
    """Bill amounts confirmed or increased after the customer's first `version` changes.

    Each `commitment_events` row counts once, so a repeated delivery of the same change adds
    nothing (SPEC §14.6, R1).
    """
    rows = reader.execute(
        "SELECT e.change, e.delta_fen FROM commitment_events e JOIN commitments c"
        " ON c.commitment_id = e.commitment_id WHERE c.customer_id = ? AND e.change <> ?"
        " ORDER BY e.commitment_event_id LIMIT -1 OFFSET ?",
        (customer_id, CANDIDATE_EVENT, version),
    ).fetchall()
    added = (CommitmentChange.CONFIRMED.value, CommitmentChange.INCREASED.value)
    return sum(max(0, row["delta_fen"]) for row in rows if row["change"] in added)


def settled_payments_fen(reader: Reader, commitment_id: str) -> int:
    row = reader.execute(
        "SELECT COALESCE(SUM(amount_fen), 0) FROM operations"
        " WHERE commitment_id = ? AND kind = ? AND status = ?",
        (commitment_id, ActionKind.PAY_BILL.value, OperationStatus.SETTLED.value),
    ).fetchone()
    return int(row[0])


def open_payment_fen(reader: Reader, commitment_id: str) -> int:
    """Amount of the bill's payments submitted and not yet settled or rejected."""
    row = reader.execute(
        "SELECT COALESCE(SUM(amount_fen), 0) FROM operations"
        " WHERE commitment_id = ? AND kind = ? AND status IN (?, ?)",
        (
            commitment_id,
            ActionKind.PAY_BILL.value,
            OperationStatus.SUBMITTED.value,
            OperationStatus.UNKNOWN.value,
        ),
    ).fetchone()
    return int(row[0])


def check_unpaid_invariant(reader: Reader, customer_id: str) -> None:
    """Raise INTEGRITY_VIOLATION when an owed bill's unpaid amount disagrees with its payments.

    The violation locks the customer's writes (K8); reads and reconciliation continue.
    """
    for commitment in customer_commitments(reader, customer_id):
        if commitment.status not in OWED:
            continue
        expected = commitment.amount_fen - settled_payments_fen(reader, commitment.commitment_id)
        if commitment.unpaid_fen != expected:
            raise WgError(
                ErrorCode.INTEGRITY_VIOLATION,
                {
                    "reason": "unpaid_mismatch",
                    "customer_id": customer_id,
                    "commitment_id": commitment.commitment_id,
                },
            )
