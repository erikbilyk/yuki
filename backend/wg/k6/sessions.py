"""Login sessions (SPEC §6). Only a hash of the session token is stored.

Session times are wall-clock times: a session belongs to a person at a browser, and the
simulated clock may jump by days or return to the epoch on reset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from wg.contracts.enums import PlanMode, Role
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, to_db


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    role: Role
    customer_id: str | None
    operator_id: str | None
    csrf_token: str
    expires_at: datetime
    revoked_at: datetime | None
    agent_mode: PlanMode = PlanMode.COORDINATED

    @property
    def subject_id(self) -> str:
        subject = self.customer_id if self.role is Role.CUSTOMER else self.operator_id
        if subject is None:
            raise ValueError(f"session {self.session_id} has no subject")
        return subject


@dataclass(frozen=True)
class Login:
    """The stored credentials of a handle: a customer or a staff member (operator or demo)."""

    subject_id: str
    role: Role
    password_hash: str
    display_name: str | None


def _session(row) -> SessionRow:
    return SessionRow(
        session_id=row["session_id"],
        role=Role(row["role"]),
        customer_id=row["customer_id"],
        operator_id=row["operator_id"],
        csrf_token=row["csrf_token"],
        expires_at=from_db(row["expires_at"]),
        revoked_at=from_db(row["revoked_at"]) if row["revoked_at"] else None,
        agent_mode=PlanMode(row["agent_mode"]),
    )


def find_login(reader: Reader, handle: str) -> Login | None:
    row = reader.execute(
        "SELECT customer_id, password_hash, display_name FROM customers WHERE handle = ?",
        (handle,),
    ).fetchone()
    if row is not None:
        return Login(row["customer_id"], Role.CUSTOMER, row["password_hash"], row["display_name"])
    row = reader.execute(
        "SELECT operator_id, role, password_hash FROM operators WHERE handle = ?", (handle,)
    ).fetchone()
    if row is not None:
        return Login(row["operator_id"], Role(row["role"]), row["password_hash"], None)
    return None


def display_name(reader: Reader, role: Role, subject_id: str) -> str | None:
    if role is not Role.CUSTOMER:
        return None
    row = reader.execute(
        "SELECT display_name FROM customers WHERE customer_id = ?", (subject_id,)
    ).fetchone()
    return None if row is None else row["display_name"]


def insert_session(
    uow: UnitOfWork,
    *,
    session_id: str,
    token_hash: str,
    login: Login,
    csrf_token: str,
    created_at: datetime,
    expires_at: datetime,
) -> SessionRow:
    customer_id = login.subject_id if login.role is Role.CUSTOMER else None
    operator_id = None if login.role is Role.CUSTOMER else login.subject_id
    uow.execute(
        "INSERT INTO sessions (session_id, token_hash, role, customer_id, operator_id,"
        " csrf_token, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            token_hash,
            login.role.value,
            customer_id,
            operator_id,
            csrf_token,
            to_db(expires_at),
            to_db(created_at),
        ),
    )
    return SessionRow(
        session_id=session_id,
        role=login.role,
        customer_id=customer_id,
        operator_id=operator_id,
        csrf_token=csrf_token,
        expires_at=from_db(to_db(expires_at)),
        revoked_at=None,
    )


def session_by_token_hash(reader: Reader, token_hash: str) -> SessionRow | None:
    row = reader.execute("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
    return None if row is None else _session(row)


def revoke_session(uow: UnitOfWork, session_id: str, at: datetime) -> bool:
    """Revoke an active session; False when it was already revoked or does not exist."""
    cursor = uow.execute(
        "UPDATE sessions SET revoked_at = ?, version = version + 1"
        " WHERE session_id = ? AND revoked_at IS NULL",
        (to_db(at), session_id),
    )
    return cursor.rowcount == 1


def set_agent_mode(uow: UnitOfWork, mode: PlanMode, at: datetime) -> int:
    """Set the agent mode of every customer session valid at `at`; returns how many."""
    cursor = uow.execute(
        "UPDATE sessions SET agent_mode = ?, version = version + 1"
        " WHERE role = 'customer' AND revoked_at IS NULL AND expires_at > ?",
        (PlanMode(mode).value, to_db(at)),
    )
    return cursor.rowcount


def delete_orphaned_sessions(uow: UnitOfWork) -> int:
    """Remove sessions whose customer or operator no longer exists (after a fixture reset)."""
    cursor = uow.execute(
        "DELETE FROM sessions WHERE"
        " (customer_id IS NOT NULL"
        "  AND customer_id NOT IN (SELECT customer_id FROM customers))"
        " OR (operator_id IS NOT NULL"
        "  AND operator_id NOT IN (SELECT operator_id FROM operators))"
    )
    return cursor.rowcount
