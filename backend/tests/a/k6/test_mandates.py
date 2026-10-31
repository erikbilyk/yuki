"""Agreement repositories, their event histories and the schema 7 constraints (S5-SPEC §4.2)."""

import sqlite3

import pytest

from wg.contracts.enums import AuditActor, EffectCheck, MandateStatus
from wg.contracts.errors import ErrorCode, WgError
from wg.k6 import mandates
from wg.k6.db import Database

from .helpers import add_customer

pytestmark = pytest.mark.layer("D")

MERCHANT = (
    "INSERT INTO merchants (merchant_id, display_name, aliases_json, settlement_account_ref,"
    " verified, created_at) VALUES (?, ?, ?, ?, ?, 't')"
)
MANDATE = (
    "INSERT INTO mandates (mandate_ref, customer_id, account_id, merchant_id, purpose_id,"
    " expected_fen, cap_fen, debit_day, starts_on, expires_on, status, created_at)"
    " VALUES (?, 'cus_main', 'acc_main', 'mer_video', ?, 2500, 3000, 31, ?, ?, ?, 't')"
)
REQUEST = (
    "INSERT INTO debit_requests (request_id, merchant_id, merchant_request_id, customer_id,"
    " mandate_ref, amount_fen, currency, period_key, description_raw, received_at, respond_by,"
    " status, reason_code, probe, created_at)"
    " VALUES (?, 'mer_video', ?, ?, ?, 2500, ?, '2026-11', '', 't', 't', ?, ?, ?, 't')"
)


def run(db: Database, sql: str, params: tuple = ()) -> None:
    with db.unit_of_work("test setup") as uow:
        uow.execute(sql, params)


def must_fail(db: Database, sql: str, params: tuple = (), match: str = "") -> None:
    with pytest.raises(sqlite3.IntegrityError, match=match):
        run(db, sql, params)


@pytest.fixture
def agreement(db: Database) -> Database:
    """One verified merchant and one ACTIVE agreement with its initial status event."""
    add_customer(db)
    with db.unit_of_work("test agreement") as uow:
        uow.execute(
            "INSERT INTO accounts (account_id, customer_id, kind, name, balance_fen, covered,"
            " created_at) VALUES ('acc_main', 'cus_main', 'customer', 'a', 0, 1, 't')"
        )
        uow.execute(MERCHANT, ("mer_video", "模拟视频会员", '["视频"]', "SIM-MER-0001", 1))
        uow.execute(MERCHANT, ("mer_ghost", "未核验商户", "[]", "SIM-MER-0099", 0))
        uow.execute(MANDATE, ("mdt_video", "pur_sub_video", "2026-10-02", "2027-10-01", "ACTIVE"))
        mandates.append_mandate_event(
            uow,
            "mdt_video",
            mandates.STATUS_FIELD,
            old=None,
            new="ACTIVE",
            actor=AuditActor.SYSTEM,
            reason="fixture",
        )
    return db


def current(db: Database) -> mandates.MandateRow:
    with db.read() as conn:
        found = mandates.find_mandate(conn, "mdt_video")
    assert found is not None
    return found


def test_reads_return_the_registry_and_the_agreement(agreement: Database):
    with agreement.read() as conn:
        registry = mandates.verified_merchants(conn)
        ghost = mandates.find_merchant(conn, "mer_ghost")
        listed = mandates.customer_mandates(conn, "cus_main")
        assert mandates.find_mandate(conn, "mdt_nothing") is None
    assert [m.merchant_id for m in registry] == ["mer_video"]
    assert registry[0].aliases == ("视频",) and registry[0].settlement_account_ref
    assert ghost is not None and not ghost.verified
    assert [m.mandate_ref for m in listed] == ["mdt_video"]
    mandate = listed[0]
    assert mandate.status is MandateStatus.ACTIVE and mandate.version == 1
    assert (mandate.starts_on.isoformat(), mandate.expires_on.isoformat()) == (
        "2026-10-02",
        "2027-10-01",
    )
    assert mandate.cancelled_at is None


def test_update_writes_one_event_per_changed_field_and_raises_the_version(agreement: Database):
    db = agreement
    updated = db.run_in_uow(
        lambda uow: mandates.update_mandate(
            uow,
            current(db),
            actor=AuditActor.CUSTOMER,
            reason="MANDATE_CAP_LOWER",
            cap_fen=2000,
            status=MandateStatus.SUSPENDED,
        ),
        "test update",
    )
    assert (updated.cap_fen, updated.status, updated.version) == (2000, "SUSPENDED", 2)
    with db.read() as conn:
        newest = mandates.newest_mandate_events(conn, "mdt_video")
        mandates.check_mandate_invariant(conn, "mdt_video")
        count = conn.execute("SELECT COUNT(*) FROM mandate_events").fetchone()[0]
    assert count == 3
    assert (newest["status"].old, newest["status"].new) == ("ACTIVE", "SUSPENDED")
    assert (newest["cap_fen"].old, newest["cap_fen"].new) == ("3000", "2000")
    assert newest["cap_fen"].actor is AuditActor.CUSTOMER
    assert newest["cap_fen"].reason == "MANDATE_CAP_LOWER"

    cancelled = db.run_in_uow(
        lambda uow: mandates.update_mandate(
            uow, updated, actor=AuditActor.CUSTOMER, reason="x", status=MandateStatus.CANCELLED
        ),
        "test cancel",
    )
    assert cancelled.status is MandateStatus.CANCELLED and cancelled.cancelled_at is not None
    assert cancelled.version == 3


def test_a_stale_version_is_refused_as_mandate_changed(agreement: Database):
    db = agreement
    stale = current(db)
    db.run_in_uow(
        lambda uow: mandates.update_mandate(
            uow, stale, actor=AuditActor.CUSTOMER, reason="x", cap_fen=2800
        ),
        "first",
    )
    with pytest.raises(WgError) as refused:
        db.run_in_uow(
            lambda uow: mandates.update_mandate(
                uow, stale, actor=AuditActor.CUSTOMER, reason="x", cap_fen=2600
            ),
            "second",
        )
    assert refused.value.code is ErrorCode.MANDATE_CHANGED
    assert refused.value.detail == {"reason": "version", "mandate_ref": "mdt_video"}
    assert current(db).cap_fen == 2800
    with pytest.raises(ValueError):
        db.run_in_uow(
            lambda uow: mandates.update_mandate(
                uow, current(db), actor=AuditActor.CUSTOMER, reason="x", cap_fen=2800
            ),
            "no change",
        )


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE mandates SET status = 'SUSPENDED'",
        "UPDATE mandates SET cap_fen = 9000",
        "DELETE FROM mandate_events",
    ],
)
def test_a_row_changed_without_its_event_is_an_integrity_violation(
    agreement: Database, tamper: str
):
    db = agreement
    if "cap_fen" in tamper:
        db.run_in_uow(
            lambda uow: mandates.update_mandate(
                uow, current(db), actor=AuditActor.CUSTOMER, reason="x", cap_fen=2800
            ),
            "cap event",
        )
    run(db, tamper)
    with db.read() as conn, pytest.raises(WgError) as broken:
        mandates.check_mandate_invariant(conn, "mdt_video")
    assert broken.value.code is ErrorCode.INTEGRITY_VIOLATION
    assert broken.value.detail == {
        "reason": "mandate_event_mismatch",
        "customer_id": "cus_main",
        "mandate_ref": "mdt_video",
    }


def test_without_a_cap_event_the_initial_cap_stands(agreement: Database):
    with agreement.read() as conn:
        assert mandates.check_mandate_invariant(conn, "mdt_video").cap_fen == 3000
        with pytest.raises(WgError) as missing:
            mandates.check_mandate_invariant(conn, "mdt_nothing")
    assert missing.value.code is ErrorCode.NOT_FOUND


def test_effect_events_append_and_the_newest_wins(agreement: Database):
    db = agreement
    run(
        db,
        "INSERT INTO operations (operation_id, idempotency_key, request_hash, kind, status,"
        " submitted_at, created_at) VALUES ('opn_1', 'k', 'h', 'MANDATE_CANCEL', 'SETTLED',"
        " 't', 't')",
    )
    with db.read() as conn:
        assert mandates.newest_effect_event(conn, "opn_1") is None
    for result, reason in ((EffectCheck.PENDING, "probe_timeout"), (EffectCheck.VERIFIED, None)):
        db.run_in_uow(
            lambda uow, r=result, why=reason: mandates.append_effect_event(
                uow, "opn_1", "probe_cancel", r, reason=why
            ),
            "effect",
        )
    with db.read() as conn:
        newest = mandates.newest_effect_event(conn, "opn_1")
    assert newest is not None and newest.result is EffectCheck.VERIFIED
    with pytest.raises(ValueError):
        db.run_in_uow(
            lambda uow: mandates.append_effect_event(uow, "opn_1", "probe_x", EffectCheck.FAILED),
            "bad kind",
        )
    must_fail(db, "UPDATE effect_verification_events SET result = 'failed'", match="append-only")


# Schema 7 constraints


def test_agreement_terms_never_change(agreement: Database):
    for change in (
        "customer_id = 'cus_main2'",
        "merchant_id = 'mer_ghost'",
        "purpose_id = 'pur_other'",
        "starts_on = '2026-10-03'",
        "expires_on = '2027-09-30'",
    ):
        must_fail(agreement, f"UPDATE mandates SET {change}", match="fixed")
    must_fail(agreement, "UPDATE mandate_events SET new = 'SUSPENDED'", match="append-only")
    run(agreement, "UPDATE mandates SET status = 'CANCELLED', cancelled_at = 'a'")
    must_fail(agreement, "UPDATE mandates SET cancelled_at = 'b'", match="fixed")


@pytest.mark.parametrize(
    ("starts", "expires", "status"),
    [
        ("2026-10-02", "2027-10-03", "ACTIVE"),
        ("2026-10-02", "2026-10-01", "ACTIVE"),
        ("2026-10-02", "2027-10-01", "PAUSED"),
        ("2026-10-02", "2027-10-01", "CANCELLED"),
    ],
)
def test_agreement_checks(agreement: Database, starts: str, expires: str, status: str):
    must_fail(agreement, MANDATE, ("mdt_two", "pur_two", starts, expires, status), match="CHECK")


def test_a_twelve_month_term_and_a_unique_purpose(agreement: Database):
    run(agreement, MANDATE, ("mdt_two", "pur_two", "2026-10-02", "2027-10-02", "ACTIVE"))
    must_fail(
        agreement,
        MANDATE,
        ("mdt_three", "pur_two", "2026-10-02", "2027-10-02", "ACTIVE"),
        match="UNIQUE",
    )


def request(request_id: str, status: str, reason: str | None = None, probe: int = 0, **kw):
    return (
        request_id,
        kw.get("merchant_request_id", request_id),
        kw.get("customer_id", "cus_main"),
        kw.get("mandate_ref", "mdt_video"),
        kw.get("currency", "CNY"),
        status,
        reason,
        probe,
    )


def test_one_open_request_per_agreement_and_period(agreement: Database):
    db = agreement
    run(db, REQUEST, request("req_1", "AWAITING_CLIENT"))
    must_fail(db, REQUEST, request("req_2", "PAID"), match="UNIQUE")
    must_fail(db, REQUEST, request("req_2", "DECLINED"), match="UNIQUE")
    run(db, REQUEST, request("req_2", "OUT_OF_MANDATE", "PERIOD_USED"))
    run(db, REQUEST, request("req_3", "AWAITING_CLIENT", probe=1))
    must_fail(db, REQUEST, request("req_4", "PAID", merchant_request_id="req_1"), match="UNIQUE")


@pytest.mark.parametrize(
    "values",
    [
        request("req_x", "AWAITING_CLIENT", "CAP"),
        request("req_x", "OUT_OF_MANDATE"),
        request("req_x", "EXPIRED"),
        request("req_x", "AUTO_DECLINED", "UNKNOWN_REASON"),
        request("req_x", "AWAITING_CLIENT", currency="USD"),
        request("req_x", "AWAITING_CLIENT", customer_id=None),
        request("req_x", "AUTO_DECLINED", "NO_MANDATE", mandate_ref=None),
        request("req_x", "AWAITING_CLIENT", probe=2),
    ],
)
def test_request_checks(agreement: Database, values: tuple):
    must_fail(agreement, REQUEST, values, match="CHECK")


def test_a_request_of_an_unknown_merchant_is_stored_without_customer_or_agreement(
    agreement: Database,
):
    run(
        agreement,
        REQUEST.replace("'mer_video'", "'mer_nobody'"),
        request("req_x", "AUTO_DECLINED", "NO_MANDATE", customer_id=None, mandate_ref=None),
    )
