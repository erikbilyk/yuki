"""Protected direct-debit periods in the cash picture, read from the database (S5-SPEC §5.6).

Golden values of S5-ACCEPTANCE §3 on a fixture_s5-shaped profile written to tmp_path. The
clock is moved directly (no sweep runs here), so where the sweep would record an expiry the
test sets the request's status itself.
"""

from datetime import date, datetime
from pathlib import Path

import pytest

from wg.contracts.api import BillsQuery
from wg.contracts.enums import AuditActor, BillStatus, CommitmentStatus, MandateStatus
from wg.contracts.facts import FactConcept
from wg.contracts.fixture import load_fixture
from wg.core.k7_verify import collect_facts
from wg.k6 import commitments, fixture, mandates
from wg.k6.cash_inputs import cash_inputs_from_db
from wg.k6.clock import advance_clock
from wg.k6.db import Database
from wg.k6.debit_requests import IncomingDebit, receive_debit_request, receive_in
from wg.k6.simtime import SHANGHAI
from wg.numeric import compute_snapshot
from wg.services.cash import CashService

from ..s5_profile import write_profile

pytestmark = pytest.mark.layer("D")

BILLS_FEN = 840_000  # rent 340,000 and insurance 500,000, unpaid throughout November
MERCHANT_OF = {"mdt_video": "mer_video", "mdt_cloud": "mer_cloud", "mdt_fitness": "mer_fitness"}


@pytest.fixture
def s5(blank_db: Database, tmp_path: Path) -> Database:
    fixture.load_into_empty(blank_db, load_fixture(write_profile(tmp_path)))
    return blank_db


def at(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=SHANGHAI)


def move(db: Database, to: datetime) -> None:
    with db.unit_of_work("test clock") as uow:
        advance_clock(uow, to)


def subscription_inputs(db: Database) -> dict[str, tuple[int, date]]:
    with db.read() as conn:
        inputs = cash_inputs_from_db(conn, "cus_main")
    return {
        c.commitment_id: (c.unpaid_fen, c.due_date)
        for c in inputs.commitments
        if c.commitment_id.startswith("cmt_sub_")
    }


def protected_sum(db: Database) -> int:
    return sum(amount for amount, _ in subscription_inputs(db).values())


def send(db: Database, ref: str, amount: int, rid: str):
    return receive_debit_request(
        db,
        IncomingDebit(
            merchant_id=MERCHANT_OF[ref],
            merchant_request_id=rid,
            mandate_ref=ref,
            amount_fen=amount,
            currency="CNY",
        ),
    )


def set_status(db: Database, request_id: str, status: str, reason: str | None) -> None:
    with db.unit_of_work("test request status") as uow:
        uow.execute(
            "UPDATE debit_requests SET status = ?, reason_code = ? WHERE request_id = ?",
            (status, reason, request_id),
        )


# §3.1 at the epoch


def test_the_snapshot_at_the_epoch_includes_the_three_november_periods(s5: Database):
    with s5.read() as conn:
        inputs = cash_inputs_from_db(conn, "cus_main")
    values = compute_snapshot(inputs)

    assert (values.c_fen, values.e_fen, values.l_fen, values.m_fen, values.u_fen) == (
        1_800_000,
        400_000,
        300_000,
        60_000,
        0,
    )
    assert values.b_fen == 864_500 == BILLS_FEN + 24_500
    assert (values.protected_fen, values.headroom_fen) == (1_624_500, 175_500)
    assert values.monthly_estimate_fen == -24_500
    subscriptions = [c for c in inputs.commitments if c.commitment_id.startswith("cmt_sub_")]
    assert [
        (c.commitment_id, c.purpose_id, c.status, c.amount_fen, c.unpaid_fen, c.due_date)
        for c in subscriptions
    ] == [
        ("cmt_sub_video_202611", "pur_sub_video", CommitmentStatus.CONFIRMED, 2_500, 2_500)
        + (date(2026, 11, 5),),
        ("cmt_sub_cloud_202611", "pur_sub_cloud", CommitmentStatus.CONFIRMED, 2_100, 2_100)
        + (date(2026, 11, 12),),
        ("cmt_sub_fitness_202611", "pur_sub_fitness", CommitmentStatus.CONFIRMED, 19_900)
        + (19_900, date(2026, 11, 20)),
    ]
    assert all(c.payee_id is None for c in subscriptions)


def test_the_snapshot_view_lists_subscriptions_apart_from_the_bills(s5: Database):
    view = CashService(s5).snapshot("cus_main")

    assert (view.headroom_fen, view.protected_fen) == (175_500, 1_624_500)
    assert (view.unpaid_bills_fen, view.unpaid_bills_count) == (BILLS_FEN, 2)
    assert view.details.b_fen == 864_500
    assert view.details.monthly_estimate_fen == -24_500
    assert {b.commitment_id for b in view.details.bills} == {"cmt_rent", "cmt_insurance"}
    assert [
        (s.mandate_ref, s.merchant_display, s.period_key, s.due_date, s.expected_fen, s.status)
        for s in view.details.subscriptions
    ] == [
        ("mdt_video", "模拟视频会员", "2026-11", date(2026, 11, 5), 2_500, BillStatus.SECURED),
        ("mdt_cloud", "模拟云盘", "2026-11", date(2026, 11, 12), 2_100, BillStatus.SECURED),
        ("mdt_fitness", "模拟健身会所", "2026-11", date(2026, 11, 20), 19_900)
        + (BillStatus.SECURED,),
    ]


def test_bills_list_and_commitment_version_do_not_see_subscriptions(s5: Database):
    service = CashService(s5)
    bills = service.bills("cus_main", BillsQuery())
    assert {b.commitment_id for b in bills.bills} == {"cmt_rent", "cmt_insurance"}
    with s5.read() as conn:
        version = commitments.commitments_version(conn, "cus_main")
    send(s5, "mdt_video", 2_500, "m-1")
    move(s5, at(11, 20))
    with s5.read() as conn:
        assert commitments.commitments_version(conn, "cus_main") == version


def test_subscription_states_get_merchant_labels_in_k7_facts(s5: Database):
    view = CashService(s5).snapshot("cus_main")
    with s5.read() as conn:
        facts = collect_facts(conn, [view], view.as_of).facts
    labels = {(f.concept, f.subject, f.label) for f in facts}
    assert (FactConcept.BILL_STATUS, "cmt_sub_video_202611", "模拟视频会员自动扣款") in labels
    assert (
        FactConcept.BILL_UNPAID,
        "cmt_sub_fitness_202611",
        "模拟健身会所自动扣款未付金额",
    ) in labels
    unpaid_total = [f for f in facts if f.label == "未支付账单合计"]
    assert [f.value for f in unpaid_total] == [BILLS_FEN]


def test_other_customer_sees_only_their_own_agreement(s5: Database):
    with s5.read() as conn:
        inputs = cash_inputs_from_db(conn, "cus_other")
    assert [c.commitment_id for c in inputs.commitments] == ["cmt_sub_other_video_202611"]


# §3.2 over time, no requests and no actions


@pytest.mark.parametrize(
    ("instant", "expected", "ids"),
    [
        (at(11, 4, 23, 59), 24_500, {"video_202611", "cloud_202611", "fitness_202611"}),
        (
            at(11, 5),
            27_000,
            {"video_202611", "cloud_202611", "fitness_202611", "video_202612"},
        ),
        (
            at(11, 6),
            27_000,
            {"video_202611", "cloud_202611", "fitness_202611", "video_202612"},
        ),
        (
            at(11, 12),
            29_100,
            {"video_202611", "cloud_202611", "fitness_202611", "video_202612", "cloud_202612"},
        ),
        (
            at(11, 20),
            49_000,
            {"video_202611", "cloud_202611", "fitness_202611"}
            | {"video_202612", "cloud_202612", "fitness_202612"},
        ),
        (
            at(11, 30, 23, 59),
            49_000,
            {"video_202611", "cloud_202611", "fitness_202611"}
            | {"video_202612", "cloud_202612", "fitness_202612"},
        ),
        (at(12, 1), 24_500, {"video_202612", "cloud_202612", "fitness_202612"}),
    ],
)
def test_protection_over_time(s5: Database, instant: datetime, expected: int, ids: set[str]):
    move(s5, instant)
    assert set(subscription_inputs(s5)) == {f"cmt_sub_{i}" for i in ids}
    assert protected_sum(s5) == expected


def test_a_period_past_its_due_date_is_overdue(s5: Database):
    move(s5, at(11, 6))
    subscriptions = CashService(s5).snapshot("cus_main").details.subscriptions
    states = {s.period_key: s.status for s in subscriptions if s.mandate_ref == "mdt_video"}
    assert states == {"2026-11": BillStatus.OVERDUE, "2026-12": BillStatus.SECURED}


# T101: fitness cancelled at the epoch


def test_t101_a_cancelled_agreement_keeps_its_period_until_the_end_of_its_day(s5: Database):
    with s5.unit_of_work("test cancel") as uow:
        fitness = mandates.find_mandate(uow, "mdt_fitness")
        assert fitness is not None
        mandates.update_mandate(
            uow,
            fitness,
            actor=AuditActor.CUSTOMER,
            reason="MANDATE_CANCEL",
            status=MandateStatus.CANCELLED,
        )
    assert protected_sum(s5) == 24_500
    move(s5, at(11, 20, 23, 59))
    assert "cmt_sub_fitness_202611" in subscription_inputs(s5)
    assert protected_sum(s5) == 29_100
    move(s5, at(11, 21))
    assert protected_sum(s5) == 9_200
    for instant in (at(11, 25), at(12, 1), at(12, 20, 12)):
        move(s5, instant)
        assert not any("fitness" in cid for cid in subscription_inputs(s5)), instant


# T111: an unanswered request, and the same after a decline


def test_t111_an_unanswered_request_protects_until_it_expires(s5: Database):
    request = send(s5, "mdt_video", 2_500, "m-first").request
    move(s5, at(11, 5, 8, 59))
    assert protected_sum(s5) == 27_000
    move(s5, at(11, 5, 9))
    set_status(s5, request.request_id, "EXPIRED", "EXPIRED")
    assert "cmt_sub_video_202611" not in subscription_inputs(s5)
    assert protected_sum(s5) == 24_500

    move(s5, at(11, 6, 9))
    later = send(s5, "mdt_video", 2_500, "m-second").request
    assert later.status.value == "AWAITING_CLIENT"
    assert "cmt_sub_video_202611" in subscription_inputs(s5)
    assert protected_sum(s5) == 27_000


def test_t111_a_declined_request_protects_until_its_deadline(s5: Database):
    request = send(s5, "mdt_video", 2_500, "m-first").request
    set_status(s5, request.request_id, "DECLINED", None)
    move(s5, at(11, 5, 8, 59))
    assert protected_sum(s5) == 27_000
    move(s5, at(11, 5, 9))
    assert protected_sum(s5) == 24_500

    move(s5, at(11, 6, 9))
    later = send(s5, "mdt_video", 2_500, "m-second").request
    assert (later.status.value, later.reason_code.value if later.reason_code else None) == (
        "OUT_OF_MANDATE",
        "PERIOD_USED",
    )
    assert protected_sum(s5) == 24_500


def test_probes_are_left_out_of_protection(s5: Database):
    with s5.unit_of_work("test probe") as uow:
        receive_in(
            uow,
            IncomingDebit("mer_video", "probe-opn_x-1", "mdt_video", 2_500, "CNY"),
            probe=True,
        )
    before = subscription_inputs(s5)
    assert protected_sum(s5) == 24_500
    # A probe awaiting the customer neither adds a period nor answers the scheduled one.
    assert before["cmt_sub_video_202611"] == (2_500, date(2026, 11, 5))
