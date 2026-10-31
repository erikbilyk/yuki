"""Receiving merchants' debit requests (S5-SPEC §5.2), judged on stored rows and audit rows.

The direct-debit profile is written to a temporary file (`tests/a/s5_profile.py`).
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from wg.contracts.enums import AuditActor, DebitReason, DebitRequestStatus, MandateStatus
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.fixture import load_fixture
from wg.core import mandate_rules
from wg.k6 import debit_requests, fixture, mandates
from wg.k6.clock import advance_clock
from wg.k6.db import Database
from wg.k6.debit_requests import IncomingDebit, receive_debit_request, receive_in
from wg.k6.simtime import SHANGHAI

from ..s5_profile import write_profile
from .helpers import EPOCH, count

pytestmark = pytest.mark.layer("D")

AWAITING = DebitRequestStatus.AWAITING_CLIENT
OUT = DebitRequestStatus.OUT_OF_MANDATE
AUTO = DebitRequestStatus.AUTO_DECLINED
MERCHANT_OF = {"mdt_video": "mer_video", "mdt_cloud": "mer_cloud", "mdt_fitness": "mer_fitness"}

_serial = iter(range(1, 10_000))


def load(db: Database, tmp_path: Path, variant: str | None = None) -> Database:
    fixture.load_into_empty(db, load_fixture(write_profile(tmp_path), variant), variant)
    return db


@pytest.fixture
def s5(blank_db: Database, tmp_path: Path) -> Database:
    return load(blank_db, tmp_path)


def at(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=SHANGHAI)


def move(db: Database, to: datetime) -> None:
    with db.unit_of_work("test clock") as uow:
        advance_clock(uow, to)


def incoming(
    ref: str = "mdt_video",
    amount: int = 2_500,
    *,
    rid: str | None = None,
    merchant: str | None = None,
    currency: str = "CNY",
    description: str = "",
) -> IncomingDebit:
    return IncomingDebit(
        merchant_id=merchant or MERCHANT_OF.get(ref, "mer_video"),
        merchant_request_id=rid or f"m-{next(_serial)}",
        mandate_ref=ref,
        amount_fen=amount,
        currency=currency,
        description_raw=description,
    )


def send(db: Database, ref: str = "mdt_video", amount: int = 2_500, **options):
    return receive_debit_request(db, incoming(ref, amount, **options))


def outcome(received) -> tuple[DebitRequestStatus, DebitReason | None]:
    return received.request.status, received.request.reason_code


def audit_rows(db: Database, event: str) -> list[tuple]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT actor, customer_id, ref, decision, reason FROM audit_log WHERE event = ?"
            " ORDER BY audit_id",
            (event,),
        ).fetchall()
    return [tuple(row) for row in rows]


def set_request_status(db: Database, request_id: str, status: str, reason: str | None) -> None:
    with db.unit_of_work("test request status") as uow:
        uow.execute(
            "UPDATE debit_requests SET status = ?, reason_code = ? WHERE request_id = ?",
            (status, reason, request_id),
        )


def change_status(db: Database, ref: str, status: MandateStatus) -> None:
    with db.unit_of_work("test agreement change") as uow:
        mandate = mandates.find_mandate(uow, ref)
        assert mandate is not None
        mandates.update_mandate(
            uow, mandate, actor=AuditActor.CUSTOMER, reason="test", status=status
        )


# Stored fields and audit


def test_an_accepted_request_is_stored_with_period_deadline_and_audit(s5: Database):
    tables = ("actions", "operations", "reservations", "domain_events", "ledger_entries")
    before = {table: count(s5, table) for table in tables}
    received = send(s5, amount=2_500, rid="m-first", description="十一月会员费")

    row = received.request
    assert received.duplicate is False
    assert (row.status, row.reason_code) == (AWAITING, None)
    assert (row.customer_id, row.mandate_ref, row.merchant_id) == (
        "cus_main",
        "mdt_video",
        "mer_video",
    )
    assert (row.amount_fen, row.currency, row.period_key) == (2_500, "CNY", "2026-11")
    assert row.received_at == EPOCH
    assert row.respond_by == EPOCH + timedelta(hours=72)
    assert row.description_raw == "十一月会员费"
    assert (row.probe, row.operation_id, row.version) == (False, None, 1)
    assert audit_rows(s5, "debit_request_received") == [
        ("system", "cus_main", row.request_id, "AWAITING_CLIENT", None)
    ]
    # Nothing else happens: no action, operation, hold or event.
    assert {table: count(s5, table) for table in tables} == before


def test_a_refused_request_is_stored_with_its_reason_and_audited(s5: Database):
    received = send(s5, amount=3_001)

    assert outcome(received) == (OUT, DebitReason.CAP)
    assert audit_rows(s5, "debit_request_received") == [
        ("system", "cus_main", received.request.request_id, "OUT_OF_MANDATE", "CAP")
    ]


# Check 1: registry and agreement


@pytest.mark.parametrize(
    ("merchant", "ref"),
    [
        ("mer_nobody", "mdt_video"),
        ("mer_unverified", "mdt_video"),
        ("mer_cloud", "mdt_video"),
        ("mer_video", "mdt_nothing"),
    ],
    ids=["unknown-merchant", "unverified-merchant", "other-merchants-agreement", "unknown-ref"],
)
def test_a_request_without_an_agreement_of_that_merchant_is_stored_without_owner(
    s5: Database, merchant: str, ref: str
):
    received = send(s5, ref, 2_500, merchant=merchant)

    row = received.request
    assert outcome(received) == (AUTO, DebitReason.NO_MANDATE)
    assert (row.customer_id, row.mandate_ref, row.merchant_id) == (None, None, merchant)
    assert audit_rows(s5, "debit_request_received") == [
        ("system", None, row.request_id, "AUTO_DECLINED", "NO_MANDATE")
    ]


# Checks 2 and 3: status and expiry


def test_a_suspended_agreement_declines(blank_db: Database, tmp_path: Path):
    db = load(blank_db, tmp_path, "video_suspended")
    received = send(db, amount=2_500)
    assert outcome(received) == (AUTO, DebitReason.MANDATE_SUSPENDED)
    assert received.request.customer_id == "cus_main"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (MandateStatus.SUSPENDED, DebitReason.MANDATE_SUSPENDED),
        (MandateStatus.CANCELLED, DebitReason.MANDATE_CANCELLED),
        (MandateStatus.EXPIRED, DebitReason.MANDATE_EXPIRED),
    ],
)
def test_an_agreement_that_is_not_active_declines(
    s5: Database, status: MandateStatus, reason: DebitReason
):
    change_status(s5, "mdt_video", status)
    received = send(s5, amount=2_500)
    assert outcome(received) == (AUTO, reason)
    assert (received.request.customer_id, received.request.mandate_ref) == (
        "cus_main",
        "mdt_video",
    )


def test_an_active_agreement_past_its_expiry_declines(blank_db: Database, tmp_path: Path):
    db = load(blank_db, tmp_path, "video_expiring")
    move(db, at(11, 5, 23, 59))
    assert outcome(send(db, amount=2_500)) == (AWAITING, None)

    move(db, at(11, 6, 0, 0))
    received = send(db, amount=2_500)
    assert outcome(received) == (AUTO, DebitReason.MANDATE_EXPIRED)
    with db.read() as conn:
        mandate = mandates.find_mandate(conn, "mdt_video")
    assert mandate is not None and mandate.status is MandateStatus.ACTIVE


# Check 4: cap


def test_the_cap_is_inclusive(s5: Database):
    assert outcome(send(s5, amount=3_000)) == (AWAITING, None)
    assert outcome(send(s5, amount=3_001)) == (OUT, DebitReason.CAP)


def test_the_cap_is_checked_before_the_period(s5: Database):
    send(s5, amount=2_500)
    assert outcome(send(s5, amount=3_001)) == (OUT, DebitReason.CAP)
    assert outcome(send(s5, amount=2_500)) == (OUT, DebitReason.PERIOD_USED)


# Check 5: one request per period


@pytest.mark.parametrize("occupying", ["AWAITING_CLIENT", "PAID", "DECLINED"])
def test_an_occupied_period_takes_no_other_request(s5: Database, occupying: str):
    first = send(s5, amount=2_500)
    set_request_status(s5, first.request.request_id, occupying, None)

    assert outcome(send(s5, amount=1_500)) == (OUT, DebitReason.PERIOD_USED)


@pytest.mark.parametrize(
    ("status", "reason"),
    [("EXPIRED", "EXPIRED"), ("AUTO_DECLINED", "MANDATE_SUSPENDED")],
)
def test_a_closed_request_leaves_the_period_free(s5: Database, status: str, reason: str):
    first = send(s5, amount=2_500)
    set_request_status(s5, first.request.request_id, status, reason)

    assert outcome(send(s5, amount=2_500)) == (AWAITING, None)


def test_a_split_amount_gets_one_request_through(s5: Database):
    assert outcome(send(s5, amount=3_000)) == (AWAITING, None)
    assert outcome(send(s5, amount=1_500)) == (OUT, DebitReason.PERIOD_USED)


def test_the_fourth_request_of_a_period_is_period_used_or_cap(s5: Database):
    results = [send(s5, amount=amount) for amount in (2_500, 2_000, 1_500, 1_000)]

    assert [outcome(r) for r in results] == [(AWAITING, None)] + [
        (OUT, DebitReason.PERIOD_USED)
    ] * 3
    assert outcome(send(s5, amount=3_001)) == (OUT, DebitReason.CAP)


def test_three_stored_requests_close_the_period_even_if_none_waits(s5: Database):
    above = [send(s5, "mdt_cloud", amount) for amount in (2_500, 2_400, 3_000)]
    assert [outcome(r) for r in above] == [(OUT, DebitReason.CAP)] * 3

    assert outcome(send(s5, "mdt_cloud", 2_100)) == (OUT, DebitReason.PERIOD_USED)
    assert outcome(send(s5, "mdt_cloud", 2_200)) == (OUT, DebitReason.CAP)


def test_requests_either_side_of_midnight_fall_into_two_periods(s5: Database):
    move(s5, at(11, 30, 23, 59))
    november = send(s5, amount=2_500)
    move(s5, at(12, 1, 0, 1))
    december = send(s5, amount=2_500)

    assert (outcome(november), november.request.period_key) == ((AWAITING, None), "2026-11")
    assert (outcome(december), december.request.period_key) == ((AWAITING, None), "2026-12")
    assert outcome(send(s5, amount=2_500)) == (OUT, DebitReason.PERIOD_USED)


def test_other_agreements_have_their_own_periods(s5: Database):
    send(s5, "mdt_video", 2_500)
    assert outcome(send(s5, "mdt_cloud", 2_100)) == (AWAITING, None)
    assert outcome(send(s5, "mdt_other_video", 2_500, merchant="mer_video")) == (AWAITING, None)


def test_a_period_index_conflict_is_stored_as_period_used(
    s5: Database, monkeypatch: pytest.MonkeyPatch
):
    # Without check 5 the partial unique index is the only guard left.
    monkeypatch.setattr(
        mandate_rules, "decide_request", lambda *a, **k: (AWAITING, None), raising=True
    )
    first = send(s5, amount=2_500)
    second = send(s5, amount=2_500)

    assert outcome(first) == (AWAITING, None)
    assert outcome(second) == (OUT, DebitReason.PERIOD_USED)
    assert audit_rows(s5, "debit_request_received")[-1][3:] == ("OUT_OF_MANDATE", "PERIOD_USED")


# Probes


def test_a_probe_neither_uses_nor_counts_against_the_period(s5: Database):
    send(s5, amount=2_500)
    with s5.unit_of_work("test probe") as uow:
        probe = receive_in(uow, incoming(amount=2_500, rid="probe-opn_x-1"), probe=True)
    assert (outcome(probe), probe.request.probe) == ((AWAITING, None), True)

    with s5.unit_of_work("test probe") as uow:
        receive_in(uow, incoming(amount=2_500, rid="probe-opn_x-2"), probe=True)
    with s5.read() as conn:
        assert debit_requests.period_statuses(conn, "mdt_video", "2026-11") == [AWAITING]


def test_a_probe_is_still_refused_by_status_and_cap(s5: Database):
    change_status(s5, "mdt_video", MandateStatus.CANCELLED)
    with s5.unit_of_work("test probe") as uow:
        probe = receive_in(uow, incoming(amount=2_500), probe=True)
    assert outcome(probe) == (AUTO, DebitReason.MANDATE_CANCELLED)

    with s5.unit_of_work("test probe") as uow:
        probe = receive_in(uow, incoming("mdt_cloud", 2_101), probe=True)
    assert outcome(probe) == (OUT, DebitReason.CAP)


# Repeats


def test_a_repeat_returns_the_original_unchanged(s5: Database):
    original = send(s5, amount=2_500, rid="m-once")
    again = send(s5, amount=2_500, rid="m-once")

    assert again.duplicate is True and again.request == original.request
    assert count(s5, "debit_requests") == 1
    assert audit_rows(s5, "debit_request_repeat_mismatch") == []
    assert len(audit_rows(s5, "debit_request_received")) == 1


def test_a_repeat_of_a_refused_request_returns_it_without_a_new_decision(s5: Database):
    original = send(s5, amount=3_001, rid="m-above")
    again = send(s5, amount=3_001, rid="m-above")
    assert again.duplicate is True and again.request == original.request
    assert outcome(again) == (OUT, DebitReason.CAP)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"amount": 2_900}, "amount"),
        ({"ref": "mdt_fitness"}, "mandate_ref"),
        ({"amount": 2_900, "ref": "mdt_fitness"}, "amount,mandate_ref"),
    ],
)
def test_a_repeat_with_other_content_is_audited_and_changes_nothing(
    s5: Database, changes: dict, reason: str
):
    original = send(s5, amount=2_500, rid="m-once")
    again = send(
        s5,
        changes.get("ref", "mdt_video"),
        changes.get("amount", 2_500),
        rid="m-once",
        merchant="mer_video",
    )

    assert again.duplicate is True and again.request == original.request
    assert audit_rows(s5, "debit_request_repeat_mismatch") == [
        ("system", "cus_main", original.request.request_id, "duplicate", reason)
    ]
    assert count(s5, "debit_requests") == 1


def test_a_repeat_in_a_later_month_returns_the_original(s5: Database):
    original = send(s5, amount=2_500, rid="m-once")
    move(s5, at(12, 2, 9))
    again = send(s5, amount=2_500, rid="m-once")

    assert again.request == original.request and again.request.period_key == "2026-11"
    assert count(s5, "debit_requests") == 1


def test_the_same_request_id_from_another_merchant_is_a_new_request(s5: Database):
    send(s5, "mdt_video", 2_500, rid="m-shared")
    other = send(s5, "mdt_cloud", 2_100, rid="m-shared")
    assert other.duplicate is False and outcome(other) == (AWAITING, None)


def test_a_repeat_of_a_request_without_agreement_compares_the_amount_only(s5: Database):
    original = send(s5, "mdt_nothing", 2_500, rid="m-stray", merchant="mer_video")
    again = send(s5, "mdt_other", 2_500, rid="m-stray", merchant="mer_video")
    assert again.request == original.request
    assert audit_rows(s5, "debit_request_repeat_mismatch") == []


# Format


@pytest.mark.parametrize(
    ("amount", "currency", "reason"),
    [(0, "CNY", "amount"), (-500, "CNY", "amount"), (2_500, "USD", "currency")],
    ids=["zero", "negative", "currency"],
)
def test_a_malformed_request_is_refused_audited_and_not_stored(
    s5: Database, amount: int, currency: str, reason: str
):
    with pytest.raises(WgError) as raised:
        send(s5, amount=amount, currency=currency, rid="m-bad")

    assert raised.value.code is ErrorCode.FORMAT_ERROR
    assert raised.value.detail == {"reason": reason}
    assert count(s5, "debit_requests") == 0
    assert audit_rows(s5, "debit_request_refused") == [
        ("system", "cus_main", "mdt_video", "FORMAT_ERROR", reason)
    ]
    # The refusal used nothing: the same merchant request id is still new.
    assert send(s5, amount=2_500, rid="m-bad").duplicate is False


def test_a_malformed_request_naming_no_agreement_is_audited_without_customer(s5: Database):
    with pytest.raises(WgError):
        send(s5, "mdt_nothing", 0, merchant="mer_video")
    assert audit_rows(s5, "debit_request_refused") == [
        ("system", None, None, "FORMAT_ERROR", "amount")
    ]


# Integrity (S5-SPEC §5.10)


def test_an_agreement_whose_row_differs_from_its_events_refuses_the_request(s5: Database):
    with s5.unit_of_work("test corruption") as uow:
        uow.execute("UPDATE mandates SET status = 'SUSPENDED' WHERE mandate_ref = 'mdt_video'")

    with pytest.raises(WgError) as raised:
        send(s5, amount=2_500)

    assert raised.value.code is ErrorCode.INTEGRITY_VIOLATION
    assert raised.value.detail["reason"] == "mandate_event_mismatch"
    assert raised.value.detail["customer_id"] == "cus_main"
    assert count(s5, "debit_requests") == 0
