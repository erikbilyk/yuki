"""Merchants, direct-debit agreements and their append-only event histories (S5-SPEC §4.2).

Every change of an agreement's status or cap is one version-checked update of the row and
one `mandate_events` row per changed field, in the caller's unit of work, so the newest
event of each field always equals the row (`check_mandate_invariant`, S5-SPEC §5.10).
The results of the probes that check a change took effect are appended to
`effect_verification_events`; the newest one per operation is the receipt's effect check.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime

from wg.contracts.enums import AuditActor, EffectCheck, MandateStatus
from wg.contracts.errors import ErrorCode, WgError
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, now_db, to_db

STATUS_FIELD = "status"
CAP_FIELD = "cap_fen"
CHECK_KINDS = frozenset({"probe_cancel", "probe_suspend", "probe_cap"})
# An agreement's expiry instant, 00:00 CST after `expires_on`, in the encoding of `to_db`.
EXPIRY_SQL = "(date(expires_on, '+1 day') || 'T00:00:00.000000+08:00')"


@dataclass(frozen=True)
class MerchantRow:
    merchant_id: str
    display_name: str
    aliases: tuple[str, ...]
    settlement_account_ref: str
    verified: bool


@dataclass(frozen=True)
class MandateRow:
    mandate_ref: str
    customer_id: str
    account_id: str
    merchant_id: str
    purpose_id: str
    expected_fen: int
    cap_fen: int
    debit_day: int
    starts_on: date
    expires_on: date
    status: MandateStatus
    cancelled_at: datetime | None
    version: int


@dataclass(frozen=True)
class MandateEventRow:
    event_id: int
    mandate_ref: str
    field: str
    old: str | None
    new: str
    operation_id: str | None
    actor: AuditActor
    reason: str | None
    at: datetime


@dataclass(frozen=True)
class EffectEventRow:
    event_id: int
    operation_id: str
    check_kind: str
    result: EffectCheck
    probe_request_id: str | None
    reason: str | None
    at: datetime


def _merchant(row) -> MerchantRow:
    return MerchantRow(
        merchant_id=row["merchant_id"],
        display_name=row["display_name"],
        aliases=tuple(json.loads(row["aliases_json"])),
        settlement_account_ref=row["settlement_account_ref"],
        verified=bool(row["verified"]),
    )


def _mandate(row) -> MandateRow:
    cancelled_at = row["cancelled_at"]
    return MandateRow(
        mandate_ref=row["mandate_ref"],
        customer_id=row["customer_id"],
        account_id=row["account_id"],
        merchant_id=row["merchant_id"],
        purpose_id=row["purpose_id"],
        expected_fen=row["expected_fen"],
        cap_fen=row["cap_fen"],
        debit_day=row["debit_day"],
        starts_on=date.fromisoformat(row["starts_on"]),
        expires_on=date.fromisoformat(row["expires_on"]),
        status=MandateStatus(row["status"]),
        cancelled_at=from_db(cancelled_at) if cancelled_at else None,
        version=row["version"],
    )


def _mandate_event(row) -> MandateEventRow:
    return MandateEventRow(
        event_id=row["event_id"],
        mandate_ref=row["mandate_ref"],
        field=row["field"],
        old=row["old"],
        new=row["new"],
        operation_id=row["operation_id"],
        actor=AuditActor(row["actor"]),
        reason=row["reason"],
        at=from_db(row["at"]),
    )


def _effect_event(row) -> EffectEventRow:
    return EffectEventRow(
        event_id=row["event_id"],
        operation_id=row["operation_id"],
        check_kind=row["check_kind"],
        result=EffectCheck(row["result"]),
        probe_request_id=row["probe_request_id"],
        reason=row["reason"],
        at=from_db(row["at"]),
    )


# Merchant registry


def find_merchant(reader: Reader, merchant_id: str) -> MerchantRow | None:
    row = reader.execute("SELECT * FROM merchants WHERE merchant_id = ?", (merchant_id,)).fetchone()
    return _merchant(row) if row else None


def verified_merchants(reader: Reader) -> list[MerchantRow]:
    """The registry: only verified merchants may hold agreements."""
    rows = reader.execute(
        "SELECT * FROM merchants WHERE verified = 1 ORDER BY merchant_id"
    ).fetchall()
    return [_merchant(row) for row in rows]


# Agreements


def find_mandate(reader: Reader, mandate_ref: str) -> MandateRow | None:
    row = reader.execute("SELECT * FROM mandates WHERE mandate_ref = ?", (mandate_ref,)).fetchone()
    return _mandate(row) if row else None


def customer_mandates(reader: Reader, customer_id: str) -> list[MandateRow]:
    rows = reader.execute(
        "SELECT * FROM mandates WHERE customer_id = ? ORDER BY starts_on, mandate_ref",
        (customer_id,),
    ).fetchall()
    return [_mandate(row) for row in rows]


def merchant_display(reader: Reader, mandate_ref: str) -> str:
    """The registry name of the agreement's merchant."""
    row = reader.execute(
        "SELECT m.display_name FROM mandates a JOIN merchants m ON m.merchant_id = a.merchant_id"
        " WHERE a.mandate_ref = ?",
        (mandate_ref,),
    ).fetchone()
    if row is None:
        raise WgError(ErrorCode.NOT_FOUND, {"mandate_ref": mandate_ref})
    return row["display_name"]


def insert_mandate(
    uow: UnitOfWork,
    *,
    mandate_ref: str,
    customer_id: str,
    account_id: str,
    merchant_id: str,
    purpose_id: str,
    expected_fen: int,
    cap_fen: int,
    debit_day: int,
    starts_on: date,
    expires_on: date,
    operation_id: str,
) -> MandateRow:
    """Store a new ACTIVE agreement with its first status event, in the caller's unit."""
    uow.execute(
        "INSERT INTO mandates (mandate_ref, customer_id, account_id, merchant_id, purpose_id,"
        " expected_fen, cap_fen, debit_day, starts_on, expires_on, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            mandate_ref,
            customer_id,
            account_id,
            merchant_id,
            purpose_id,
            expected_fen,
            cap_fen,
            debit_day,
            starts_on.isoformat(),
            expires_on.isoformat(),
            MandateStatus.ACTIVE.value,
            now_db(uow),
        ),
    )
    append_mandate_event(
        uow,
        mandate_ref,
        STATUS_FIELD,
        old=None,
        new=MandateStatus.ACTIVE.value,
        actor=AuditActor.CUSTOMER,
        reason="MANDATE_CREATE",
        operation_id=operation_id,
    )
    created = find_mandate(uow, mandate_ref)
    assert created is not None
    return created


def update_mandate(
    uow: UnitOfWork,
    mandate: MandateRow,
    *,
    actor: AuditActor,
    reason: str,
    status: MandateStatus | None = None,
    cap_fen: int | None = None,
    operation_id: str | None = None,
) -> MandateRow:
    """Change status and/or cap of an agreement still at `mandate.version`.

    Appends one `mandate_events` row per changed field and raises the version by one;
    a CANCELLED status records `cancelled_at`. An agreement that changed since it was read
    is refused with MANDATE_CHANGED (reason `version`).
    """
    changes: list[tuple[str, str, str]] = []
    if status is not None and status is not mandate.status:
        changes.append((STATUS_FIELD, mandate.status.value, MandateStatus(status).value))
    if cap_fen is not None and cap_fen != mandate.cap_fen:
        changes.append((CAP_FIELD, str(mandate.cap_fen), str(cap_fen)))
    if not changes:
        raise ValueError(f"update of {mandate.mandate_ref} changes nothing")
    new_status = MandateStatus(status) if status is not None else mandate.status
    cancelled_at = now_db(uow) if new_status is MandateStatus.CANCELLED else None
    cursor = uow.execute(
        "UPDATE mandates SET status = ?, cap_fen = ?, cancelled_at = COALESCE(cancelled_at, ?),"
        " version = version + 1 WHERE mandate_ref = ? AND version = ?",
        (
            new_status.value,
            cap_fen if cap_fen is not None else mandate.cap_fen,
            cancelled_at,
            mandate.mandate_ref,
            mandate.version,
        ),
    )
    if cursor.rowcount != 1:
        raise WgError(
            ErrorCode.MANDATE_CHANGED, {"reason": "version", "mandate_ref": mandate.mandate_ref}
        )
    for field, old, new in changes:
        append_mandate_event(
            uow,
            mandate.mandate_ref,
            field,
            old=old,
            new=new,
            actor=actor,
            reason=reason,
            operation_id=operation_id,
        )
    updated = find_mandate(uow, mandate.mandate_ref)
    assert updated is not None
    return updated


def append_mandate_event(
    uow: UnitOfWork,
    mandate_ref: str,
    field: str,
    *,
    old: str | None,
    new: str,
    actor: AuditActor,
    reason: str | None,
    operation_id: str | None = None,
) -> int:
    cursor = uow.execute(
        "INSERT INTO mandate_events (mandate_ref, field, old, new, operation_id, actor, reason,"
        " at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (mandate_ref, field, old, new, operation_id, AuditActor(actor).value, reason, now_db(uow)),
    )
    return int(cursor.lastrowid or 0)


def newest_mandate_events(reader: Reader, mandate_ref: str) -> dict[str, MandateEventRow]:
    """The newest event of each field of the agreement."""
    rows = reader.execute(
        "SELECT e.* FROM mandate_events e WHERE e.mandate_ref = ? AND e.event_id = ("
        " SELECT MAX(event_id) FROM mandate_events"
        " WHERE mandate_ref = e.mandate_ref AND field = e.field)",
        (mandate_ref,),
    ).fetchall()
    return {row["field"]: _mandate_event(row) for row in rows}


def check_mandate_invariant(reader: Reader, mandate_ref: str) -> MandateRow:
    """The agreement, after checking that its newest events agree with its row (§5.10).

    A mismatch means the row was changed outside `update_mandate`: INTEGRITY_VIOLATION with
    the customer, which the failure policy turns into a customer lock.
    """
    mandate = find_mandate(reader, mandate_ref)
    if mandate is None:
        raise WgError(ErrorCode.NOT_FOUND, {"mandate_ref": mandate_ref})
    newest = newest_mandate_events(reader, mandate_ref)
    status = newest.get(STATUS_FIELD)
    cap = newest.get(CAP_FIELD)
    if (
        status is None
        or status.new != mandate.status.value
        or (cap is not None and cap.new != str(mandate.cap_fen))
    ):
        raise WgError(
            ErrorCode.INTEGRITY_VIOLATION,
            {
                "reason": "mandate_event_mismatch",
                "customer_id": mandate.customer_id,
                "mandate_ref": mandate_ref,
            },
        )
    return mandate


# Effect checks of agreement changes


def append_effect_event(
    uow: UnitOfWork,
    operation_id: str,
    check_kind: str,
    result: EffectCheck,
    *,
    probe_request_id: str | None = None,
    reason: str | None = None,
) -> int:
    if check_kind not in CHECK_KINDS:
        raise ValueError(f"unknown effect check {check_kind!r}")
    cursor = uow.execute(
        "INSERT INTO effect_verification_events (operation_id, check_kind, result,"
        " probe_request_id, reason, at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            operation_id,
            check_kind,
            EffectCheck(result).value,
            probe_request_id,
            reason,
            now_db(uow),
        ),
    )
    return int(cursor.lastrowid or 0)


def newest_effect_event(reader: Reader, operation_id: str) -> EffectEventRow | None:
    row = reader.execute(
        "SELECT * FROM effect_verification_events WHERE operation_id = ?"
        " ORDER BY event_id DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    return _effect_event(row) if row else None


def unverified_changes(
    reader: Reader, kinds: Collection[str], customer_id: str | None = None
) -> list[str]:
    """SETTLED change operations of `kinds` whose newest effect check is missing or pending.

    A change that a later event of the same agreement has overtaken is left out: a probe sent
    now would test the agreement as it stands, not the change.
    """
    marks = ", ".join("?" for _ in kinds)
    sql = (
        f"SELECT o.operation_id FROM operations o WHERE o.kind IN ({marks})"
        " AND o.status = 'SETTLED' AND COALESCE((SELECT e.result"
        "  FROM effect_verification_events e WHERE e.operation_id = o.operation_id"
        "  ORDER BY e.event_id DESC LIMIT 1), 'pending') = 'pending'"
        " AND NOT EXISTS (SELECT 1 FROM mandate_events own JOIN mandate_events later"
        "  ON later.mandate_ref = own.mandate_ref AND later.event_id > own.event_id"
        "  WHERE own.operation_id = o.operation_id"
        "  AND COALESCE(later.operation_id, '') <> o.operation_id)"
        + (" AND o.customer_id = ?" if customer_id is not None else "")
        + " ORDER BY o.rowid"
    )
    params: list[object] = list(kinds)
    if customer_id is not None:
        params.append(customer_id)
    return [row["operation_id"] for row in reader.execute(sql, params).fetchall()]


def effect_event_count(reader: Reader, operation_id: str) -> int:
    row = reader.execute(
        "SELECT COUNT(*) FROM effect_verification_events WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    return int(row[0])


def ended_mandates(reader: Reader, at: datetime, customer_id: str | None = None) -> list[str]:
    """ACTIVE or SUSPENDED agreements whose expiry instant is at or before `at`."""
    sql = (
        f"SELECT mandate_ref FROM mandates WHERE status IN ('ACTIVE', 'SUSPENDED')"
        f" AND {EXPIRY_SQL} <= ?"
        + (" AND customer_id = ?" if customer_id is not None else "")
        + " ORDER BY expires_on, mandate_ref"
    )
    params: list[object] = [to_db(at)]
    if customer_id is not None:
        params.append(customer_id)
    return [row["mandate_ref"] for row in reader.execute(sql, params).fetchall()]
