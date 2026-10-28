"""Step-up challenges, grants and the simulated SMS outbox (SPEC §9.3).

A challenge is open until it is verified or invalidated. A verified challenge that is not
consumed or invalidated is a grant. The plaintext code lives in `sms_outbox` only while
the challenge is open; it is wiped when the challenge closes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, now_db, to_db


@dataclass(frozen=True)
class ChallengeRow:
    challenge_id: str
    customer_id: str
    approval_id: str
    logical_action_id: str
    revision: int
    scope_hash: str
    code_hmac: str
    attempts: int
    max_attempts: int
    issued_at: datetime
    expires_at: datetime
    verified_at: datetime | None
    consumed_at: datetime | None
    invalidated_at: datetime | None
    invalidation_reason: str | None
    version: int

    @property
    def is_open(self) -> bool:
        return self.verified_at is None and self.invalidated_at is None

    @property
    def is_grant(self) -> bool:
        return (
            self.verified_at is not None
            and self.consumed_at is None
            and self.invalidated_at is None
        )

    @property
    def attempts_left(self) -> int:
        return max(self.max_attempts - self.attempts, 0)


@dataclass(frozen=True)
class SmsRow:
    challenge_id: str
    customer_id: str
    code: str
    created_at: datetime


def _opt(text: str | None) -> datetime | None:
    return from_db(text) if text else None


def _challenge(row) -> ChallengeRow:
    return ChallengeRow(
        challenge_id=row["challenge_id"],
        customer_id=row["customer_id"],
        approval_id=row["approval_id"],
        logical_action_id=row["logical_action_id"],
        revision=row["revision"],
        scope_hash=row["scope_hash"],
        code_hmac=row["code_hmac"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        issued_at=from_db(row["issued_at"]),
        expires_at=from_db(row["expires_at"]),
        verified_at=_opt(row["verified_at"]),
        consumed_at=_opt(row["consumed_at"]),
        invalidated_at=_opt(row["invalidated_at"]),
        invalidation_reason=row["invalidation_reason"],
        version=row["version"],
    )


def insert_challenge(
    uow: UnitOfWork,
    *,
    challenge_id: str,
    customer_id: str,
    approval_id: str,
    logical_action_id: str,
    revision: int,
    scope_hash: str,
    code_hmac: str,
    max_attempts: int,
    issued_at: datetime,
    expires_at: datetime,
) -> ChallengeRow:
    uow.execute(
        "INSERT INTO challenges (challenge_id, customer_id, approval_id, logical_action_id,"
        " revision, scope_hash, code_hmac, max_attempts, issued_at, expires_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            challenge_id,
            customer_id,
            approval_id,
            logical_action_id,
            revision,
            scope_hash,
            code_hmac,
            max_attempts,
            to_db(issued_at),
            to_db(expires_at),
            now_db(uow),
        ),
    )
    return get_challenge(uow, challenge_id)


def find_challenge(reader: Reader, challenge_id: str) -> ChallengeRow | None:
    row = reader.execute(
        "SELECT * FROM challenges WHERE challenge_id = ?", (challenge_id,)
    ).fetchone()
    return None if row is None else _challenge(row)


def get_challenge(reader: Reader, challenge_id: str) -> ChallengeRow:
    challenge = find_challenge(reader, challenge_id)
    if challenge is None:
        raise LookupError(f"no challenge {challenge_id}")
    return challenge


def challenges_of_action(
    reader: Reader, logical_action_id: str, revision: int | None = None
) -> list[ChallengeRow]:
    """Challenges of a logical action (optionally one revision), oldest first."""
    if revision is None:
        rows = reader.execute(
            "SELECT * FROM challenges WHERE logical_action_id = ? ORDER BY rowid",
            (logical_action_id,),
        ).fetchall()
    else:
        rows = reader.execute(
            "SELECT * FROM challenges WHERE logical_action_id = ? AND revision = ? ORDER BY rowid",
            (logical_action_id, revision),
        ).fetchall()
    return [_challenge(row) for row in rows]


def active_challenges(
    reader: Reader,
    *,
    customer_id: str | None = None,
    plan_id: str | None = None,
    logical_action_id: str | None = None,
) -> list[ChallengeRow]:
    """Open challenges and grants (not invalidated, not consumed) matching every filter given."""
    sql = (
        "SELECT c.* FROM challenges c JOIN actions a"
        " ON a.logical_action_id = c.logical_action_id AND a.revision = c.revision"
        " WHERE c.invalidated_at IS NULL AND c.consumed_at IS NULL"
    )
    params: list[str] = []
    for column, value in (
        ("c.customer_id", customer_id),
        ("a.plan_id", plan_id),
        ("c.logical_action_id", logical_action_id),
    ):
        if value is not None:
            sql += f" AND {column} = ?"
            params.append(value)
    rows = reader.execute(sql + " ORDER BY c.rowid", params).fetchall()
    return [_challenge(row) for row in rows]


def record_failed_attempt(uow: UnitOfWork, challenge: ChallengeRow) -> ChallengeRow:
    cursor = uow.execute(
        "UPDATE challenges SET attempts = attempts + 1, version = version + 1"
        " WHERE challenge_id = ? AND version = ?",
        (challenge.challenge_id, challenge.version),
    )
    _changed(cursor.rowcount, challenge)
    return get_challenge(uow, challenge.challenge_id)


def mark_verified(uow: UnitOfWork, challenge: ChallengeRow, at: datetime) -> ChallengeRow:
    cursor = uow.execute(
        "UPDATE challenges SET verified_at = ?, version = version + 1"
        " WHERE challenge_id = ? AND version = ? AND verified_at IS NULL"
        " AND invalidated_at IS NULL",
        (to_db(at), challenge.challenge_id, challenge.version),
    )
    _changed(cursor.rowcount, challenge)
    return get_challenge(uow, challenge.challenge_id)


def mark_consumed(uow: UnitOfWork, challenge: ChallengeRow, at: datetime) -> ChallengeRow:
    cursor = uow.execute(
        "UPDATE challenges SET consumed_at = ?, version = version + 1"
        " WHERE challenge_id = ? AND version = ? AND verified_at IS NOT NULL"
        " AND consumed_at IS NULL AND invalidated_at IS NULL",
        (to_db(at), challenge.challenge_id, challenge.version),
    )
    _changed(cursor.rowcount, challenge)
    return get_challenge(uow, challenge.challenge_id)


def invalidate(uow: UnitOfWork, challenge: ChallengeRow, at: datetime, reason: str) -> ChallengeRow:
    """Close an open challenge or an unconsumed grant; the caller withdraws its SMS code."""
    cursor = uow.execute(
        "UPDATE challenges SET invalidated_at = ?, invalidation_reason = ?,"
        " version = version + 1"
        " WHERE challenge_id = ? AND version = ? AND invalidated_at IS NULL"
        " AND consumed_at IS NULL",
        (to_db(at), reason, challenge.challenge_id, challenge.version),
    )
    _changed(cursor.rowcount, challenge)
    return get_challenge(uow, challenge.challenge_id)


def expired_open(reader: Reader, at: datetime) -> list[ChallengeRow]:
    """Open challenges whose five minutes are over at `at`."""
    rows = reader.execute(
        "SELECT * FROM challenges WHERE verified_at IS NULL AND invalidated_at IS NULL"
        " AND expires_at <= ? ORDER BY rowid",
        (to_db(at),),
    ).fetchall()
    return [_challenge(row) for row in rows]


def _changed(rowcount: int, challenge: ChallengeRow) -> None:
    if rowcount != 1:
        raise RuntimeError(f"challenge {challenge.challenge_id} changed concurrently")


# Simulated SMS outbox


def insert_sms(uow: UnitOfWork, customer_id: str, challenge_id: str, code: str) -> None:
    uow.execute(
        "INSERT INTO sms_outbox (customer_id, challenge_id, code, created_at) VALUES (?, ?, ?, ?)",
        (customer_id, challenge_id, code, now_db(uow)),
    )


def wipe_code(uow: UnitOfWork, challenge_id: str) -> None:
    uow.execute(
        "UPDATE sms_outbox SET code = NULL, version = version + 1"
        " WHERE challenge_id = ? AND code IS NOT NULL",
        (challenge_id,),
    )


def open_sms(reader: Reader, customer_id: str, at: datetime) -> list[SmsRow]:
    """Codes of the customer's open challenges that have not expired at `at`, newest first."""
    rows = reader.execute(
        "SELECT s.challenge_id, s.customer_id, s.code, s.created_at FROM sms_outbox s"
        " JOIN challenges c ON c.challenge_id = s.challenge_id"
        " WHERE s.customer_id = ? AND s.code IS NOT NULL"
        " AND c.verified_at IS NULL AND c.invalidated_at IS NULL AND c.expires_at > ?"
        " ORDER BY s.sms_id DESC",
        (customer_id, to_db(at)),
    ).fetchall()
    return [
        SmsRow(
            challenge_id=row["challenge_id"],
            customer_id=row["customer_id"],
            code=row["code"],
            created_at=from_db(row["created_at"]),
        )
        for row in rows
    ]
