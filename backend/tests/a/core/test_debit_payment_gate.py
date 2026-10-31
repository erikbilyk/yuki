"""The payment gate of DEBIT_PAY and the decline check, in their order (S5-SPEC §5.4, §5.5).

The packs' prechecks run on a database holding the direct-debit profile; each case changes
one fact and expects the first refusal it causes.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wg.contracts.enums import ActionKind, AuditActor, DebitRequestStatus, MandateStatus
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.fixture import load_fixture
from wg.core import k3_policy
from wg.k6 import bank, debit_requests, fixture, mandates
from wg.k6.clock import advance_clock
from wg.k6.db import Database
from wg.k6.debit_requests import DebitRequestRow, IncomingDebit, receive_debit_request
from wg.k6.migrate import migrate
from wg.k6.simtime import SHANGHAI
from wg.packs.base import Infeasible
from wg.packs.mandates import DebitDeclinePack, DebitPayPack

from ..s5_profile import write_profile

pytestmark = pytest.mark.layer("D")

MAIN = "cus_main"
EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)
PAY = DebitPayPack()
DECLINE = DebitDeclinePack()
_serial = iter(range(1, 10_000))


def open_db(tmp_path: Path, variant: str | None = None) -> Database:
    db = Database(tmp_path / "wg.db")
    migrate(db)
    fixture.load_into_empty(db, load_fixture(write_profile(tmp_path), variant), variant)
    return db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_db(tmp_path)
    yield database
    database.close()


def send(db: Database, ref: str = "mdt_video", amount: int = 2_500) -> DebitRequestRow:
    merchant = "mer_video" if ref in ("mdt_video", "mdt_other_video") else ref.replace("mdt", "mer")
    return receive_debit_request(
        db,
        IncomingDebit(
            merchant_id=merchant,
            merchant_request_id=f"g-{next(_serial)}",
            mandate_ref=ref,
            amount_fen=amount,
            currency="CNY",
        ),
    ).request


def pay_params(db: Database, request: DebitRequestRow, **override: Any) -> dict[str, Any]:
    with db.read() as conn:
        mandate = mandates.find_mandate(conn, str(request.mandate_ref))
    assert mandate is not None
    params = {
        "account_id": mandate.account_id,
        "mandate_ref": mandate.mandate_ref,
        "mandate_version": mandate.version,
        "request_id": request.request_id,
        "amount_fen": request.amount_fen,
    }
    return PAY.canonical(ActionKind.DEBIT_PAY, {**params, **override})


def check_pay(
    db: Database, params: dict[str, Any], at: datetime = EPOCH, customer_id: str = MAIN
) -> tuple[Infeasible, ...]:
    with db.read() as conn:
        return PAY.precheck(conn, customer_id, ActionKind.DEBIT_PAY, params, at)


def codes(reasons: tuple[Infeasible, ...]) -> list[tuple[str, Any]]:
    return [(r.code.value, r.detail.get("reason")) for r in reasons]


def change(db: Database, ref: str, **fields: Any) -> None:
    with db.unit_of_work("test agreement change") as uow:
        mandate = mandates.find_mandate(uow, ref)
        assert mandate is not None
        mandates.update_mandate(uow, mandate, actor=AuditActor.CUSTOMER, reason="test", **fields)


def move(db: Database, to: datetime) -> None:
    with db.unit_of_work("test clock") as uow:
        advance_clock(uow, to)


# The gate in order


def test_a_waiting_request_within_the_agreement_passes(db: Database):
    request = send(db)
    assert check_pay(db, pay_params(db, request)) == ()


def test_the_agreements_integrity_is_checked_before_anything_else(db: Database):
    above = send(db, amount=3_001)
    with db.unit_of_work("test corruption") as uow:
        uow.execute("UPDATE mandates SET status = 'SUSPENDED' WHERE mandate_ref = 'mdt_video'")

    with pytest.raises(WgError) as refused:
        check_pay(db, pay_params(db, above))

    assert refused.value.code is ErrorCode.INTEGRITY_VIOLATION
    assert refused.value.detail["reason"] == "mandate_event_mismatch"


def test_a_foreign_or_probe_request_is_not_found(db: Database):
    foreign = send(db, "mdt_other_video")
    with pytest.raises(WgError) as refused:
        check_pay(db, pay_params(db, foreign))
    assert (refused.value.code, refused.value.detail) == (ErrorCode.NOT_FOUND, {})

    own = send(db)
    with db.unit_of_work("test probe") as uow:
        uow.execute("UPDATE debit_requests SET probe = 1 WHERE request_id = ?", (own.request_id,))
    with pytest.raises(WgError) as probe:
        check_pay(db, pay_params(db, own))
    assert probe.value.code is ErrorCode.NOT_FOUND


def test_a_request_that_no_longer_waits_is_refused_first(db: Database):
    above = send(db, amount=3_001)
    # Also a changed agreement: the request's state decides first.
    reasons = check_pay(db, pay_params(db, above, mandate_version=9))
    assert codes(reasons) == [("REQUEST_NOT_PAYABLE", "request_not_awaiting")]
    assert reasons[0].detail["request_id"] == above.request_id


def test_respond_by_is_exclusive(db: Database):
    request = send(db)
    params = pay_params(db, request)
    assert check_pay(db, params, at=request.respond_by - timedelta(microseconds=1)) == ()
    assert codes(check_pay(db, params, at=request.respond_by)) == [
        ("REQUEST_NOT_PAYABLE", "respond_by_passed")
    ]


def test_a_changed_version_comes_before_the_agreements_status(db: Database):
    request = send(db)
    old = pay_params(db, request)
    change(db, "mdt_video", status=MandateStatus.SUSPENDED)

    stale = check_pay(db, old)
    current = check_pay(db, pay_params(db, request))

    assert codes(stale) == [("MANDATE_CHANGED", "version")]
    assert stale[0].detail["mandate_ref"] == "mdt_video"
    assert codes(current) == [("REQUEST_NOT_PAYABLE", "mandate_not_active")]


def test_an_expired_agreement_cannot_pay_while_its_request_waits(tmp_path: Path):
    db = open_db(tmp_path, "video_expiring")
    move(db, datetime(2026, 11, 5, 8, 0, tzinfo=SHANGHAI))
    request = send(db)
    params = pay_params(db, request)
    expiry = datetime(2026, 11, 6, 0, 0, tzinfo=SHANGHAI)

    assert check_pay(db, params, at=expiry - timedelta(microseconds=1)) == ()
    assert codes(check_pay(db, params, at=expiry)) == [("REQUEST_NOT_PAYABLE", "mandate_expired")]
    db.close()


def test_an_amount_above_a_lowered_cap_is_refused(db: Database):
    request = send(db, amount=2_500)
    change(db, "mdt_video", cap_fen=2_000)
    assert codes(check_pay(db, pay_params(db, request))) == [("REQUEST_NOT_PAYABLE", "over_cap")]
    change(db, "mdt_video", cap_fen=2_500)
    assert check_pay(db, pay_params(db, request)) == ()


def test_a_paid_period_and_a_payment_in_flight_are_backstops(
    db: Database, monkeypatch: pytest.MonkeyPatch
):
    # Both cannot arise from stored rows: a paid request leaves the period occupied and
    # the request that was paid no longer waits. The checks stand behind those rules.
    request = send(db)
    params = pay_params(db, request)
    monkeypatch.setattr(debit_requests, "payment_operation_ids", lambda reader, rid: ["opn_x"])
    assert codes(check_pay(db, params)) == [("REQUEST_NOT_PAYABLE", "payment_in_flight")]
    monkeypatch.setattr(
        debit_requests,
        "period_statuses",
        lambda reader, ref, key: [DebitRequestStatus.AWAITING_CLIENT, DebitRequestStatus.PAID],
    )
    assert codes(check_pay(db, params)) == [("REQUEST_NOT_PAYABLE", "period_paid")]


def test_the_money_reasons_are_listed_together_in_order(
    db: Database, monkeypatch: pytest.MonkeyPatch
):
    request = send(db, amount=3_000)
    with db.unit_of_work("test drain") as uow:
        bank.post_external_debit(
            uow,
            customer_id=MAIN,
            account_id="acc_main",
            amount_fen=1_798_000,
            merchant="外部扣款",
            at=EPOCH,
        )
    monkeypatch.setattr(k3_policy, "daily_sum", lambda reader, customer, day: 9_998_000)

    reasons = check_pay(db, pay_params(db, request))

    assert [r.code for r in reasons] == [
        ErrorCode.DAILY_LIMIT_EXCEEDED,
        ErrorCode.INSUFFICIENT_FUNDS,
        ErrorCode.INSUFFICIENT_HEADROOM,
    ]
    assert reasons[1].detail == {"available_fen": 2_000, "shortfall_fen": 1_000}
    assert reasons[2].detail["over_headroom_fen"] == 500 - reasons[2].detail["headroom_fen"]


def test_params_that_differ_from_the_stored_request_are_a_defect(db: Database):
    request = send(db)
    with pytest.raises(ValueError):
        check_pay(db, pay_params(db, request, amount_fen=2_400))


# The decline


def test_a_decline_needs_a_request_that_still_waits(db: Database):
    request = send(db)
    above = send(db, "mdt_cloud", amount=2_200)
    params = {"mandate_ref": "mdt_video", "request_id": request.request_id}

    with db.read() as conn:
        assert DECLINE.precheck(conn, MAIN, ActionKind.DEBIT_DECLINE, params, EPOCH) == ()
        late = DECLINE.precheck(conn, MAIN, ActionKind.DEBIT_DECLINE, params, request.respond_by)
        closed = DECLINE.precheck(
            conn,
            MAIN,
            ActionKind.DEBIT_DECLINE,
            {"mandate_ref": "mdt_cloud", "request_id": above.request_id},
            EPOCH,
        )

    assert codes(late) == [("REQUEST_NOT_PAYABLE", "respond_by_passed")]
    assert codes(closed) == [("REQUEST_NOT_PAYABLE", "request_not_awaiting")]


def test_declining_marks_the_request_once(db: Database):
    request = send(db)
    with db.unit_of_work("test decline") as uow:
        declined = debit_requests.decline(uow, request.request_id)
    assert (declined.status, declined.version) == (DebitRequestStatus.DECLINED, request.version + 1)

    with pytest.raises(WgError) as again:
        with db.unit_of_work("test decline again") as uow:
            debit_requests.decline(uow, request.request_id)
    assert again.value.code is ErrorCode.REQUEST_NOT_PAYABLE


def test_the_hashes_cover_the_business_fields_only(db: Database):
    request = send(db)
    params = pay_params(db, request)
    same = PAY.request_hash(ActionKind.DEBIT_PAY, MAIN, params)
    assert PAY.request_hash(ActionKind.DEBIT_PAY, MAIN, {**params, "mandate_version": 7}) == same
    assert PAY.request_hash(ActionKind.DEBIT_PAY, MAIN, {**params, "amount_fen": 1}) != same
    decline = {"mandate_ref": "mdt_video", "request_id": request.request_id}
    assert DECLINE.request_hash(ActionKind.DEBIT_DECLINE, MAIN, decline) != DECLINE.request_hash(
        ActionKind.DEBIT_DECLINE, MAIN, {**decline, "request_id": "req_other"}
    )
    with pytest.raises(ValueError):
        PAY.canonical(ActionKind.DEBIT_PAY, {**params, "payee_id": "pay_mom"})
