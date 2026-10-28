from datetime import UTC, datetime, timedelta

import pytest

from wg.contracts.enums import PayeeBank, Role
from wg.contracts.errors import ErrorCode, WgError
from wg.core.k1_session import (
    SESSION_TTL,
    SessionService,
    mask_account,
    owned,
    payee_handle,
)
from wg.k6.db import Database

pytestmark = pytest.mark.layer("D")

SECRET = "test-session-secret-" + "1" * 32


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(db: Database, clock: Clock) -> SessionService:
    return SessionService(db, SECRET, clock)


def test_login_opens_a_session_of_the_handles_role(service: SessionService, db: Database):
    entry, token = service.login("demo-main", "wg-demo-main-2026")
    assert entry.role is Role.CUSTOMER and entry.subject_id == "cus_main"
    assert entry.display_name == "周宁"
    principal = service.resolve(token, Role.CUSTOMER)
    assert principal is not None and principal.customer_id == "cus_main"
    assert principal.session.csrf_token == entry.csrf_token
    with db.read() as conn:
        stored = [tuple(row) for row in conn.execute("SELECT token_hash, csrf_token FROM sessions")]
    # Only the HMAC of the cookie token is stored.
    assert stored == [(service.token_hash(token), entry.csrf_token)]
    assert token not in stored[0][0]


def test_staff_logins_get_their_roles(service: SessionService):
    operator, _ = service.login("demo-ops", "wg-demo-ops-2026")
    demo, _ = service.login("demo-panel", "wg-demo-panel-2026")
    assert (operator.role, operator.subject_id) == (Role.OPERATOR, "opr_ops")
    assert (demo.role, demo.subject_id) == (Role.DEMO, "opr_demo")


def test_unknown_handle_and_wrong_password_are_refused_alike(service: SessionService, db: Database):
    refusals = []
    for handle, password in (("demo-main", "wrong"), ("nobody", "wg-demo-main-2026")):
        with pytest.raises(WgError) as refused:
            service.login(handle, password)
        refusals.append((refused.value.code, refused.value.detail))
    assert refusals[0] == refusals[1] == (ErrorCode.SESSION_EXPIRED, {"reason": "bad_credentials"})
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        events = [
            row[0] for row in conn.execute("SELECT event FROM audit_log WHERE customer_id IS NULL")
        ]
    assert events.count("login_refused") == 2


def test_a_session_serves_only_its_role(service: SessionService):
    _, token = service.login("demo-panel", "wg-demo-panel-2026")
    assert service.resolve(token, Role.DEMO) is not None
    assert service.resolve(token, Role.CUSTOMER) is None
    assert service.resolve(token, Role.OPERATOR) is None
    assert service.resolve("not-a-token", Role.DEMO) is None
    assert service.resolve(None, Role.DEMO) is None


def test_session_expires_on_the_wall_clock(service: SessionService, clock: Clock):
    _, token = service.login("demo-main", "wg-demo-main-2026")
    clock.now += SESSION_TTL - timedelta(seconds=1)
    assert service.resolve(token, Role.CUSTOMER) is not None
    clock.now += timedelta(seconds=1)
    assert service.resolve(token, Role.CUSTOMER) is None


def test_logout_and_relogin_revoke_the_old_session(service: SessionService):
    _, first = service.login("demo-main", "wg-demo-main-2026")
    _, second = service.login("demo-main", "wg-demo-main-2026", {Role.CUSTOMER: first})
    assert service.resolve(first, Role.CUSTOMER) is None
    principal = service.resolve(second, Role.CUSTOMER)
    assert principal is not None
    service.logout(principal)
    assert service.resolve(second, Role.CUSTOMER) is None


def test_another_secret_does_not_resolve_the_token(db: Database, service: SessionService):
    _, token = service.login("demo-main", "wg-demo-main-2026")
    assert SessionService(db, "another-secret").resolve(token, Role.CUSTOMER) is None


@pytest.mark.case("T54")
def test_owned_gives_one_refusal_for_foreign_and_missing():
    record = {"id": "pln_b", "customer_id": "cus_other"}
    assert owned(record, "cus_other", lambda r: r["customer_id"]) is record
    refusals = []
    for candidate in (record, None):
        with pytest.raises(WgError) as refused:
            owned(candidate, "cus_main", lambda r: r["customer_id"])
        refusals.append((refused.value.code, refused.value.detail, refused.value.http_status))
    assert refusals == [(ErrorCode.NOT_FOUND, {}, 404)] * 2


def test_masked_handles():
    assert payee_handle("妈妈", PayeeBank.SAME, "··1234") == "妈妈 · 本行 ··1234"
    assert payee_handle("爸爸", PayeeBank.OTHER, "··5678") == "爸爸 · 他行 ··5678"
    assert mask_account("6222 0200 1234 5678") == "··5678"
