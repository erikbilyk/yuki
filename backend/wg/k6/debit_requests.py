"""Merchants' debit requests: receiving them and reading them back (S5-SPEC §4.2, §5.2).

A request is decided once, when it arrives, and stored with that outcome; nothing else
happens then (no action, hold or notification). `description_raw` is the merchant's text,
stored as received and never read as an instruction.

Afterwards a request leaves AWAITING_CLIENT once: paid (S5-SPEC §6.2) or declined (§5.5).
Each change raises its version and is audited in the same unit of work.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from wg.contracts.enums import AuditActor, DebitReason, DebitRequestStatus
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.ids import IdPrefix, new_id
from wg.core import mandate_rules
from wg.k6 import audit, mandates
from wg.k6.db import Database, Reader, UnitOfWork
from wg.k6.simtime import from_db, now, to_db

# The partial unique index that keeps one occupying request per agreement and period.
_PERIOD_INDEX_COLUMNS = "debit_requests.mandate_ref, debit_requests.period_key"


@dataclass(frozen=True)
class IncomingDebit:
    """A request as the merchant sent it; every field is the merchant's claim."""

    merchant_id: str
    merchant_request_id: str
    mandate_ref: str
    amount_fen: int
    currency: str
    description_raw: str = ""


@dataclass(frozen=True)
class DebitRequestRow:
    request_id: str
    merchant_id: str
    merchant_request_id: str
    customer_id: str | None
    mandate_ref: str | None
    amount_fen: int
    currency: str
    period_key: str
    description_raw: str
    received_at: datetime
    respond_by: datetime
    status: DebitRequestStatus
    reason_code: DebitReason | None
    operation_id: str | None
    probe: bool
    version: int


@dataclass(frozen=True)
class PaidRequest:
    mandate_ref: str
    amount_fen: int
    paid_at: datetime


@dataclass(frozen=True)
class Received:
    request: DebitRequestRow
    duplicate: bool


def _request(row) -> DebitRequestRow:
    reason = row["reason_code"]
    return DebitRequestRow(
        request_id=row["request_id"],
        merchant_id=row["merchant_id"],
        merchant_request_id=row["merchant_request_id"],
        customer_id=row["customer_id"],
        mandate_ref=row["mandate_ref"],
        amount_fen=row["amount_fen"],
        currency=row["currency"],
        period_key=row["period_key"],
        description_raw=row["description_raw"],
        received_at=from_db(row["received_at"]),
        respond_by=from_db(row["respond_by"]),
        status=DebitRequestStatus(row["status"]),
        reason_code=DebitReason(reason) if reason else None,
        operation_id=row["operation_id"],
        probe=bool(row["probe"]),
        version=row["version"],
    )


# Receiving


def receive_debit_request(db: Database, incoming: IncomingDebit) -> Received:
    """Check, store and audit one merchant request in its own unit of work (S5-SPEC §5.2).

    A malformed amount or currency is refused with FORMAT_ERROR; the refusal is audited in
    a unit of its own and nothing is stored.
    """
    problem = mandate_rules.format_problem(incoming.amount_fen, incoming.currency)
    if problem is not None:
        db.run_in_uow(lambda uow: _audit_refusal(uow, incoming, problem), "debit request refused")
        raise WgError(ErrorCode.FORMAT_ERROR, {"reason": problem})
    return db.run_in_uow(lambda uow: receive_in(uow, incoming), "debit request")


def receive_in(uow: UnitOfWork, incoming: IncomingDebit, *, probe: bool = False) -> Received:
    """The unit body of `receive_debit_request` for a well-formed request; probes use it too.

    A repeated `(merchant_id, merchant_request_id)` returns the stored request unchanged.
    When the named agreement exists, its event history is checked first (S5-SPEC §5.10).
    """
    original = find_by_merchant_request(uow, incoming.merchant_id, incoming.merchant_request_id)
    if original is not None:
        _audit_repeat_mismatch(uow, original, incoming)
        return Received(original, duplicate=True)
    mandate = mandates.find_mandate(uow, incoming.mandate_ref)
    if mandate is not None:
        mandate = mandates.check_mandate_invariant(uow, mandate.mandate_ref)
    merchant = mandates.find_merchant(uow, incoming.merchant_id)
    at = now(uow)
    key = mandate_rules.period_key(at)
    status, reason = mandate_rules.decide_request(
        mandate,
        merchant_verified=merchant is not None and merchant.verified,
        merchant_id=incoming.merchant_id,
        amount_fen=incoming.amount_fen,
        now=at,
        period_statuses=period_statuses(uow, incoming.mandate_ref, key) if mandate else (),
        probe=probe,
    )
    owner = mandate if reason is not DebitReason.NO_MANDATE else None
    row = _store(
        uow,
        incoming,
        customer_id=owner.customer_id if owner else None,
        mandate_ref=owner.mandate_ref if owner else None,
        at=at,
        status=status,
        reason=reason,
        probe=probe,
    )
    audit.record(
        uow,
        AuditActor.SYSTEM,
        "debit_request_received",
        customer_id=row.customer_id,
        ref=row.request_id,
        decision=row.status.value,
        reason=row.reason_code.value if row.reason_code else None,
    )
    return Received(row, duplicate=False)


def _store(
    uow: UnitOfWork,
    incoming: IncomingDebit,
    *,
    customer_id: str | None,
    mandate_ref: str | None,
    at: datetime,
    status: DebitRequestStatus,
    reason: DebitReason | None,
    probe: bool,
) -> DebitRequestRow:
    """Insert the request; a conflict on the one-per-period index stores it as PERIOD_USED."""
    try:
        return _insert(uow, incoming, customer_id, mandate_ref, at, status, reason, probe)
    except sqlite3.IntegrityError as exc:
        if _PERIOD_INDEX_COLUMNS not in str(exc):
            raise
    return _insert(
        uow,
        incoming,
        customer_id,
        mandate_ref,
        at,
        DebitRequestStatus.OUT_OF_MANDATE,
        DebitReason.PERIOD_USED,
        probe,
    )


def _insert(
    uow: UnitOfWork,
    incoming: IncomingDebit,
    customer_id: str | None,
    mandate_ref: str | None,
    at: datetime,
    status: DebitRequestStatus,
    reason: DebitReason | None,
    probe: bool,
) -> DebitRequestRow:
    request_id = new_id(IdPrefix.DEBIT_REQUEST)
    received_at = to_db(at)
    uow.execute(
        "INSERT INTO debit_requests (request_id, merchant_id, merchant_request_id, customer_id,"
        " mandate_ref, amount_fen, currency, period_key, description_raw, received_at,"
        " respond_by, status, reason_code, probe, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            request_id,
            incoming.merchant_id,
            incoming.merchant_request_id,
            customer_id,
            mandate_ref,
            incoming.amount_fen,
            incoming.currency,
            mandate_rules.period_key(at),
            incoming.description_raw,
            received_at,
            to_db(mandate_rules.respond_by(at)),
            status.value,
            reason.value if reason else None,
            int(probe),
            received_at,
        ),
    )
    row = find_request(uow, request_id)
    assert row is not None
    return row


def _audit_refusal(uow: UnitOfWork, incoming: IncomingDebit, problem: str) -> None:
    mandate = mandates.find_mandate(uow, incoming.mandate_ref)
    audit.record(
        uow,
        AuditActor.SYSTEM,
        "debit_request_refused",
        customer_id=mandate.customer_id if mandate else None,
        ref=mandate.mandate_ref if mandate else None,
        decision=ErrorCode.FORMAT_ERROR.value,
        reason=problem,
    )


def _audit_repeat_mismatch(
    uow: UnitOfWork, original: DebitRequestRow, incoming: IncomingDebit
) -> None:
    """Audit a repeat whose amount or agreement differs from the stored request.

    A request stored without an agreement (NO_MANDATE) keeps no ref, so only its amount
    can be compared.
    """
    differences = []
    if incoming.amount_fen != original.amount_fen:
        differences.append("amount")
    if original.mandate_ref is not None and incoming.mandate_ref != original.mandate_ref:
        differences.append("mandate_ref")
    if differences:
        audit.record(
            uow,
            AuditActor.SYSTEM,
            "debit_request_repeat_mismatch",
            customer_id=original.customer_id,
            ref=original.request_id,
            decision="duplicate",
            reason=",".join(differences),
        )


# Answers


def mark_paid(uow: UnitOfWork, request_id: str, operation_id: str) -> DebitRequestRow:
    """The bank paid the request through `operation_id`."""
    return _close(
        uow,
        request_id,
        DebitRequestStatus.PAID,
        None,
        operation_id=operation_id,
        actor=AuditActor.SYSTEM,
        event="debit_request_paid",
    )


def decline(uow: UnitOfWork, request_id: str) -> DebitRequestRow:
    """The customer declined the request; the period stays occupied."""
    return _close(
        uow,
        request_id,
        DebitRequestStatus.DECLINED,
        None,
        actor=AuditActor.CUSTOMER,
        event="debit_request_declined",
    )


def _close(
    uow: UnitOfWork,
    request_id: str,
    status: DebitRequestStatus,
    reason: DebitReason | None,
    *,
    actor: AuditActor,
    event: str,
    operation_id: str | None = None,
) -> DebitRequestRow:
    """Move a request out of AWAITING_CLIENT; one that already left it is not payable."""
    cursor = uow.execute(
        "UPDATE debit_requests SET status = ?, reason_code = ?, operation_id = ?,"
        " version = version + 1 WHERE request_id = ? AND status = ?",
        (
            status.value,
            reason.value if reason else None,
            operation_id,
            request_id,
            DebitRequestStatus.AWAITING_CLIENT.value,
        ),
    )
    if cursor.rowcount != 1:
        raise WgError(
            ErrorCode.REQUEST_NOT_PAYABLE,
            {"reason": "request_not_awaiting", "request_id": request_id},
        )
    row = find_request(uow, request_id)
    assert row is not None
    audit.record(
        uow,
        actor,
        event,
        customer_id=row.customer_id,
        ref=request_id,
        decision=status.value,
        reason=reason.value if reason else None,
    )
    return row


# Reads


def find_request(reader: Reader, request_id: str) -> DebitRequestRow | None:
    row = reader.execute(
        "SELECT * FROM debit_requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    return _request(row) if row else None


def find_by_merchant_request(
    reader: Reader, merchant_id: str, merchant_request_id: str
) -> DebitRequestRow | None:
    row = reader.execute(
        "SELECT * FROM debit_requests WHERE merchant_id = ? AND merchant_request_id = ?",
        (merchant_id, merchant_request_id),
    ).fetchone()
    return _request(row) if row else None


def period_statuses(reader: Reader, mandate_ref: str, key: str) -> list[DebitRequestStatus]:
    """Statuses of the agreement's stored non-probe requests in period `key`."""
    rows = reader.execute(
        "SELECT status FROM debit_requests WHERE mandate_ref = ? AND period_key = ? AND probe = 0",
        (mandate_ref, key),
    ).fetchall()
    return [DebitRequestStatus(row["status"]) for row in rows]


def payment_operation_ids(reader: Reader, request_id: str) -> list[str]:
    """Operations of DEBIT_PAY actions that pay this request, whatever their status."""
    rows = reader.execute(
        "SELECT o.operation_id FROM operations o JOIN actions a"
        " ON a.logical_action_id = o.logical_action_id AND a.revision = o.revision"
        " WHERE a.kind = 'DEBIT_PAY' AND json_extract(a.params_json, '$.request_id') = ?"
        " ORDER BY o.rowid",
        (request_id,),
    ).fetchall()
    return [row["operation_id"] for row in rows]


def mandate_requests(reader: Reader, mandate_ref: str) -> list[DebitRequestRow]:
    """The agreement's non-probe requests in arrival order."""
    rows = reader.execute(
        "SELECT * FROM debit_requests WHERE mandate_ref = ? AND probe = 0"
        " ORDER BY received_at, rowid",
        (mandate_ref,),
    ).fetchall()
    return [_request(row) for row in rows]


def customer_requests(
    reader: Reader,
    customer_id: str,
    *,
    status: DebitRequestStatus | None = None,
    mandate_ref: str | None = None,
) -> list[DebitRequestRow]:
    """The customer's non-probe requests, newest first (arrival order within one instant)."""
    sql = "SELECT * FROM debit_requests WHERE customer_id = ? AND probe = 0"
    params: list[object] = [customer_id]
    if status is not None:
        sql += " AND status = ?"
        params.append(status.value)
    if mandate_ref is not None:
        sql += " AND mandate_ref = ?"
        params.append(mandate_ref)
    rows = reader.execute(sql + " ORDER BY received_at DESC, rowid DESC", params).fetchall()
    return [_request(row) for row in rows]


def paid_requests(reader: Reader, customer_id: str, since: datetime) -> list[PaidRequest]:
    """The customer's PAID requests whose payment was submitted since `since`, oldest first."""
    rows = reader.execute(
        "SELECT r.mandate_ref, r.amount_fen, o.submitted_at FROM debit_requests r"
        " JOIN operations o ON o.operation_id = r.operation_id"
        " WHERE r.customer_id = ? AND r.probe = 0 AND r.status = ? AND o.submitted_at >= ?"
        " ORDER BY o.submitted_at, r.rowid",
        (customer_id, DebitRequestStatus.PAID.value, to_db(since)),
    ).fetchall()
    return [
        PaidRequest(
            mandate_ref=row["mandate_ref"],
            amount_fen=row["amount_fen"],
            paid_at=from_db(row["submitted_at"]),
        )
        for row in rows
    ]
