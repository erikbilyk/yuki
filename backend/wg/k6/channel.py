"""IM channel records: pairing codes, bindings and seen messages (SPEC §7, §21)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, now_db, to_db


@dataclass(frozen=True)
class PairingCodeRow:
    code_hmac: str
    customer_id: str
    expires_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True)
class BindingRow:
    binding_id: int
    channel: str
    external_user_id: str
    customer_id: str
    bound_at: datetime


def insert_pairing_code(
    uow: UnitOfWork, code_hmac: str, customer_id: str, expires_at: datetime
) -> bool:
    """Store a new code; False when a live code already has the same HMAC."""
    cursor = uow.execute(
        "INSERT OR IGNORE INTO im_pairing_codes (code_hmac, customer_id, expires_at, created_at)"
        " VALUES (?, ?, ?, ?)",
        (code_hmac, customer_id, to_db(expires_at), now_db(uow)),
    )
    return cursor.rowcount == 1


def close_open_codes(uow: UnitOfWork, customer_id: str, at: datetime) -> None:
    """End the customer's unused, unexpired codes, so only the newest one can bind."""
    uow.execute(
        "UPDATE im_pairing_codes SET expires_at = ?"
        " WHERE customer_id = ? AND consumed_at IS NULL AND expires_at > ?",
        (to_db(at), customer_id, to_db(at)),
    )


def find_pairing_code(reader: Reader, code_hmac: str) -> PairingCodeRow | None:
    row = reader.execute(
        "SELECT code_hmac, customer_id, expires_at, consumed_at FROM im_pairing_codes"
        " WHERE code_hmac = ?",
        (code_hmac,),
    ).fetchone()
    if row is None:
        return None
    return PairingCodeRow(
        code_hmac=row["code_hmac"],
        customer_id=row["customer_id"],
        expires_at=from_db(row["expires_at"]),
        consumed_at=from_db(row["consumed_at"]) if row["consumed_at"] else None,
    )


def consume_pairing_code(uow: UnitOfWork, code_hmac: str, at: datetime) -> bool:
    """Mark the code used; False when it was already used or has expired."""
    cursor = uow.execute(
        "UPDATE im_pairing_codes SET consumed_at = ?"
        " WHERE code_hmac = ? AND consumed_at IS NULL AND expires_at > ?",
        (to_db(at), code_hmac, to_db(at)),
    )
    return cursor.rowcount == 1


def active_binding(reader: Reader, channel: str, external_user_id: str) -> BindingRow | None:
    row = reader.execute(
        "SELECT binding_id, channel, external_user_id, customer_id, bound_at FROM im_bindings"
        " WHERE channel = ? AND external_user_id = ? AND revoked_at IS NULL",
        (channel, external_user_id),
    ).fetchone()
    if row is None:
        return None
    return BindingRow(
        binding_id=row["binding_id"],
        channel=row["channel"],
        external_user_id=row["external_user_id"],
        customer_id=row["customer_id"],
        bound_at=from_db(row["bound_at"]),
    )


def revoke_binding(uow: UnitOfWork, channel: str, external_user_id: str, at: datetime) -> None:
    uow.execute(
        "UPDATE im_bindings SET revoked_at = ?"
        " WHERE channel = ? AND external_user_id = ? AND revoked_at IS NULL",
        (to_db(at), channel, external_user_id),
    )


def insert_binding(
    uow: UnitOfWork, channel: str, external_user_id: str, customer_id: str, at: datetime
) -> None:
    uow.execute(
        "INSERT INTO im_bindings (channel, external_user_id, customer_id, bound_at, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (channel, external_user_id, customer_id, to_db(at), to_db(at)),
    )


def mark_seen(uow: UnitOfWork, channel: str, message_id: str) -> bool:
    """Record a delivered message; False when the same ID was seen before."""
    cursor = uow.execute(
        "INSERT OR IGNORE INTO im_messages_seen (channel, message_id, created_at) VALUES (?, ?, ?)",
        (channel, message_id, now_db(uow)),
    )
    return cursor.rowcount == 1


def newest_unapproved_card(reader: Reader, customer_id: str, at: datetime) -> str | None:
    """The customer's newest card that is not approved and has not expired."""
    row = reader.execute(
        "SELECT card_id FROM cards WHERE customer_id = ? AND approval_id IS NULL"
        " AND expires_at > ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (customer_id, to_db(at)),
    ).fetchone()
    return None if row is None else row["card_id"]
