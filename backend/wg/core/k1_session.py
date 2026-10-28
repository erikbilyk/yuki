"""K1: login, sessions, CSRF tokens, ownership and masked handles (SPEC §6).

Each role has its own cookie, so the customer screen, the operator screen and the demo
panel can be open in one browser. The cookie holds a random token; the database keeps only
its HMAC under WG_SESSION_SECRET. Every state-changing request carries the CSRF token of
the role the route needs.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache

from wg.contracts.api import SessionEntry
from wg.contracts.enums import AuditActor, PayeeBank, Role
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.ids import IdPrefix, new_id
from wg.k6 import audit, sessions
from wg.k6.db import Database, UnitOfWork
from wg.k6.fixture import is_empty
from wg.k6.passwords import hash_password, verify_password
from wg.k6.sessions import SessionRow

SESSION_TTL = timedelta(hours=12)

COOKIE_NAMES: dict[Role, str] = {
    Role.CUSTOMER: "wg_session",
    Role.DEMO: "wg_demo",
    Role.OPERATOR: "wg_operator",
}

CSRF_HEADER = "X-CSRF-Token"

BANK_LABELS: dict[PayeeBank, str] = {PayeeBank.SAME: "本行", PayeeBank.OTHER: "他行"}

# The demo panel drives the simulator, so its rows are system rows.
_AUDIT_ACTORS: dict[Role, AuditActor] = {
    Role.CUSTOMER: AuditActor.CUSTOMER,
    Role.OPERATOR: AuditActor.OPERATOR,
    Role.DEMO: AuditActor.SYSTEM,
}


@dataclass(frozen=True)
class Principal:
    """The holder of a valid session: who is acting and in which role."""

    session: SessionRow

    @property
    def role(self) -> Role:
        return self.session.role

    @property
    def subject_id(self) -> str:
        return self.session.subject_id

    @property
    def customer_id(self) -> str:
        if self.session.customer_id is None:
            raise WgError(ErrorCode.FORBIDDEN_ROUTE, {"role": self.role.value})
        return self.session.customer_id

    @property
    def audit_actor(self) -> AuditActor:
        return _AUDIT_ACTORS[self.role]

    def csrf_matches(self, presented: str | None) -> bool:
        return presented is not None and hmac.compare_digest(
            presented.encode("utf-8"), self.session.csrf_token.encode("utf-8")
        )


@cache
def _decoy_hash() -> str:
    """A hash to check unknown handles against, so both refusals cost the same time."""
    return hash_password(secrets.token_urlsafe(16))


def wall_clock() -> datetime:
    return datetime.now(UTC)


class SessionService:
    def __init__(
        self, db: Database, session_secret: str, clock: Callable[[], datetime] = wall_clock
    ) -> None:
        if not session_secret:
            raise ValueError("the session secret is empty")
        self.db = db
        self._secret = session_secret.encode("utf-8")
        self._clock = clock

    def token_hash(self, token: str) -> str:
        return hmac.new(self._secret, token.encode("utf-8"), "sha256").hexdigest()

    def login(
        self, handle: str, password: str, held_tokens: Mapping[Role, str] | None = None
    ) -> tuple[SessionEntry, str]:
        """Check the credentials and open a session; returns its entry and the cookie token.

        An unknown handle and a wrong password get the same refusal. `held_tokens` are the
        cookies this browser already holds; the session of the same role is revoked.
        """
        with self.db.read() as conn:
            login = sessions.find_login(conn, handle)
            empty = is_empty(conn)
        # scrypt is slow; it runs before the unit of work opens.
        valid = verify_password(password, login.password_hash if login else _decoy_hash())
        if login is None or not valid:
            if not empty:
                self.db.run_in_uow(
                    lambda uow: audit.record(
                        uow, AuditActor.SYSTEM, "login_refused", reason="bad_credentials"
                    ),
                    "login refused",
                )
            raise WgError(ErrorCode.SESSION_EXPIRED, {"reason": "bad_credentials"})
        token = secrets.token_urlsafe(32)
        replaced = self.resolve((held_tokens or {}).get(login.role), login.role)

        def open_session(uow: UnitOfWork) -> SessionRow:
            now = self._clock()
            if replaced is not None:
                sessions.revoke_session(uow, replaced.session.session_id, now)
            row = sessions.insert_session(
                uow,
                session_id=new_id(IdPrefix.SESSION),
                token_hash=self.token_hash(token),
                login=login,
                csrf_token=secrets.token_urlsafe(24),
                created_at=now,
                expires_at=now + SESSION_TTL,
            )
            audit.record(
                uow,
                Principal(row).audit_actor,
                "login",
                customer_id=row.customer_id,
                ref=row.session_id,
                reason=login.role.value,
            )
            return row

        row = self.db.run_in_uow(open_session, "login")
        return self.entry(Principal(row)), token

    def resolve(self, token: str | None, role: Role) -> Principal | None:
        """The principal of a cookie token, if its session is valid and has `role`."""
        if not token:
            return None
        with self.db.read() as conn:
            row = sessions.session_by_token_hash(conn, self.token_hash(token))
        if row is None or row.role is not role or row.revoked_at is not None:
            return None
        if row.expires_at <= self._clock():
            return None
        return Principal(row)

    def logout(self, principal: Principal) -> None:
        def close(uow: UnitOfWork) -> None:
            sessions.revoke_session(uow, principal.session.session_id, self._clock())
            audit.record(
                uow,
                principal.audit_actor,
                "logout",
                customer_id=principal.session.customer_id,
                ref=principal.session.session_id,
                reason=principal.role.value,
            )

        self.db.run_in_uow(close, "logout")

    def entry(self, principal: Principal) -> SessionEntry:
        with self.db.read() as conn:
            name = sessions.display_name(conn, principal.role, principal.subject_id)
        session = principal.session
        return SessionEntry(
            role=session.role,
            subject_id=session.subject_id,
            display_name=name,
            csrf_token=session.csrf_token,
            expires_at=session.expires_at,
        )


# Ownership (SPEC §6, T54)


def not_found() -> WgError:
    """The one refusal for objects that are foreign or do not exist; it names nothing."""
    return WgError(ErrorCode.NOT_FOUND)


def owned[T](record: T | None, customer_id: str, owner: Callable[[T], str | None]) -> T:
    """`record` if it exists and belongs to `customer_id`; otherwise the NOT_FOUND refusal."""
    if record is None or owner(record) != customer_id:
        raise not_found()
    return record


# Masked display (SPEC §5.2)


def mask_account(account_number: str) -> str:
    """Only the last four digits, as in `··1234`."""
    digits = "".join(ch for ch in account_number if ch.isdigit())
    return "··" + digits[-4:]


def payee_handle(alias: str, bank: PayeeBank, account_masked: str) -> str:
    """The handle the model and the customer see, such as `妈妈 · 本行 ··1234`."""
    return f"{alias} · {BANK_LABELS[PayeeBank(bank)]} {account_masked}"
