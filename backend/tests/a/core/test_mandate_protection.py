"""The request decision (S5-SPEC §5.2) and protected periods (§5.6) as pure functions.

Golden values from S5-ACCEPTANCE §3 for cus_main's three agreements of fixture_s5: video
2,500 on day 5, cloud 2,100 on day 12, fitness 19,900 on day 20, all from 2026-10-02 to
2027-10-01.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

import pytest

from wg.contracts.enums import DebitReason, DebitRequestStatus, MandateStatus
from wg.core.mandate_rules import (
    decide_request,
    expected_amount,
    format_problem,
    next_debit_date,
    parse_subscription_commitment_id,
    protected_periods,
    subscription_commitment_id,
)
from wg.k6.mandates import MandateRow
from wg.k6.simtime import SHANGHAI

S = DebitRequestStatus
R = DebitReason
EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)


def at(month: int, day: int, hour: int = 0, minute: int = 0, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=SHANGHAI)


def agreement(ref: str, expected: int, cap: int, day: int, **changes) -> MandateRow:
    row = MandateRow(
        mandate_ref=ref,
        customer_id="cus_main",
        account_id="acc_main",
        merchant_id=ref.replace("mdt_", "mer_"),
        purpose_id=ref.replace("mdt_", "pur_sub_"),
        expected_fen=expected,
        cap_fen=cap,
        debit_day=day,
        starts_on=date(2026, 10, 2),
        expires_on=date(2027, 10, 1),
        status=MandateStatus.ACTIVE,
        cancelled_at=None,
        version=1,
    )
    return replace(row, **changes)


VIDEO = agreement("mdt_video", 2_500, 3_000, 5)
CLOUD = agreement("mdt_cloud", 2_100, 2_100, 12)
FITNESS = agreement("mdt_fitness", 19_900, 22_000, 20)


@dataclass(frozen=True)
class Request:
    period_key: str
    status: DebitRequestStatus
    amount_fen: int
    respond_by: datetime


def request(status: DebitRequestStatus, amount: int = 2_500, received: datetime = EPOCH) -> Request:
    key = f"{received.year:04d}-{received.month:02d}"
    return Request(key, status, amount, received + timedelta(hours=72))


def periods(mandate: MandateRow, now: datetime, *requests: Request) -> list[tuple[str, int]]:
    return [(p.period_key, p.expected_fen) for p in protected_periods(mandate, requests, now)]


def total(
    now: datetime,
    video=(),
    cloud=(),
    fitness=(),
    agreements: dict[str, MandateRow] | None = None,
) -> int:
    """The protected sum of the three agreements at `now`; `agreements` replaces some."""
    agreements = agreements or {}
    return sum(
        p.expected_fen
        for mandate, requests in (
            (agreements.get("video", VIDEO), video),
            (agreements.get("cloud", CLOUD), cloud),
            (agreements.get("fitness", FITNESS), fitness),
        )
        for p in protected_periods(mandate, requests, now)
    )


# Format and decision table (§5.2)


def test_format_problems():
    assert format_problem(1, "CNY") is None
    assert format_problem(0, "CNY") == "amount"
    assert format_problem(-500, "CNY") == "amount"
    assert format_problem(2_500, "USD") == "currency"
    assert format_problem(0, "USD") == "amount"


def decide(
    mandate: MandateRow | None = VIDEO,
    *,
    amount: int = 2_500,
    verified: bool = True,
    merchant: str = "mer_video",
    now: datetime = EPOCH,
    period: tuple[DebitRequestStatus, ...] = (),
    probe: bool = False,
):
    return decide_request(
        mandate,
        merchant_verified=verified,
        merchant_id=merchant,
        amount_fen=amount,
        now=now,
        period_statuses=period,
        probe=probe,
    )


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, (S.AWAITING_CLIENT, None)),
        ({"verified": False}, (S.AUTO_DECLINED, R.NO_MANDATE)),
        ({"mandate": None}, (S.AUTO_DECLINED, R.NO_MANDATE)),
        ({"merchant": "mer_cloud"}, (S.AUTO_DECLINED, R.NO_MANDATE)),
        (
            {"mandate": replace(VIDEO, status=MandateStatus.SUSPENDED)},
            (S.AUTO_DECLINED, R.MANDATE_SUSPENDED),
        ),
        (
            {"mandate": replace(VIDEO, status=MandateStatus.CANCELLED, cancelled_at=EPOCH)},
            (S.AUTO_DECLINED, R.MANDATE_CANCELLED),
        ),
        (
            {"mandate": replace(VIDEO, status=MandateStatus.EXPIRED)},
            (S.AUTO_DECLINED, R.MANDATE_EXPIRED),
        ),
        (
            {"mandate": replace(VIDEO, expires_on=date(2026, 11, 5)), "now": at(11, 6)},
            (S.AUTO_DECLINED, R.MANDATE_EXPIRED),
        ),
        (
            {"mandate": replace(VIDEO, expires_on=date(2026, 11, 5)), "now": at(11, 5, 23, 59)},
            (S.AWAITING_CLIENT, None),
        ),
        ({"amount": 3_000}, (S.AWAITING_CLIENT, None)),
        ({"amount": 3_001}, (S.OUT_OF_MANDATE, R.CAP)),
        ({"amount": 3_001, "period": (S.AWAITING_CLIENT,)}, (S.OUT_OF_MANDATE, R.CAP)),
        ({"period": (S.AWAITING_CLIENT,)}, (S.OUT_OF_MANDATE, R.PERIOD_USED)),
        ({"period": (S.PAID,)}, (S.OUT_OF_MANDATE, R.PERIOD_USED)),
        ({"period": (S.DECLINED,)}, (S.OUT_OF_MANDATE, R.PERIOD_USED)),
        ({"period": (S.EXPIRED, S.AUTO_DECLINED)}, (S.AWAITING_CLIENT, None)),
        ({"period": (S.OUT_OF_MANDATE,) * 2}, (S.AWAITING_CLIENT, None)),
        ({"period": (S.OUT_OF_MANDATE,) * 3}, (S.OUT_OF_MANDATE, R.PERIOD_USED)),
        ({"period": (S.OUT_OF_MANDATE,) * 3, "amount": 3_001}, (S.OUT_OF_MANDATE, R.CAP)),
        ({"period": (S.AWAITING_CLIENT,), "probe": True}, (S.AWAITING_CLIENT, None)),
        ({"period": (S.OUT_OF_MANDATE,) * 3, "probe": True}, (S.AWAITING_CLIENT, None)),
        ({"amount": 3_001, "probe": True}, (S.OUT_OF_MANDATE, R.CAP)),
    ],
    ids=[
        "accepted",
        "unverified-merchant",
        "no-agreement",
        "other-merchant",
        "suspended",
        "cancelled",
        "expired-status",
        "past-expiry",
        "expiry-minus-a-minute",
        "at-cap",
        "above-cap",
        "cap-before-period",
        "awaiting-in-period",
        "paid-in-period",
        "declined-in-period",
        "closed-requests-free",
        "two-stored",
        "three-stored",
        "three-stored-above-cap",
        "probe-ignores-period",
        "probe-ignores-count",
        "probe-above-cap",
    ],
)
def test_the_first_failing_check_decides(options: dict, expected: tuple):
    assert decide(**options) == expected


def test_status_is_checked_before_expiry_and_cap():
    suspended_and_expired = replace(
        VIDEO, status=MandateStatus.SUSPENDED, expires_on=date(2026, 11, 1)
    )
    assert decide(suspended_and_expired, amount=9_999) == (S.AUTO_DECLINED, R.MANDATE_SUSPENDED)
    expired = replace(VIDEO, expires_on=date(2026, 11, 1))
    assert decide(expired, amount=9_999) == (S.AUTO_DECLINED, R.MANDATE_EXPIRED)


# Commitment ids


def test_commitment_ids_of_protected_periods_round_trip():
    assert subscription_commitment_id("mdt_video", "2026-11") == "cmt_sub_video_202611"
    assert parse_subscription_commitment_id("cmt_sub_video_202611") == ("mdt_video", "2026-11")
    assert subscription_commitment_id("mdt_other_video", "2026-12") == (
        "cmt_sub_other_video_202612"
    )
    assert parse_subscription_commitment_id("cmt_sub_other_video_202612") == (
        "mdt_other_video",
        "2026-12",
    )
    longest = "mdt_" + "a" * 40
    assert parse_subscription_commitment_id(subscription_commitment_id(longest, "2027-01")) == (
        longest,
        "2027-01",
    )
    for stored in ("cmt_rent", "cmt_0123456789abcdef", "cmt_sub_video", "cmt_sub__202611"):
        assert parse_subscription_commitment_id(stored) is None


# Next debit date


def test_next_debit_date_is_on_or_after_today_within_the_term():
    assert next_debit_date(VIDEO, date(2026, 11, 2)) == date(2026, 11, 5)
    assert next_debit_date(VIDEO, date(2026, 11, 5)) == date(2026, 11, 5)
    assert next_debit_date(VIDEO, date(2026, 11, 6)) == date(2026, 12, 5)
    day_31 = replace(VIDEO, debit_day=31)
    assert next_debit_date(day_31, date(2027, 2, 1)) == date(2027, 2, 28)
    assert next_debit_date(VIDEO, date(2027, 10, 1)) is None
    late_start = replace(VIDEO, starts_on=date(2026, 11, 10), expires_on=date(2027, 11, 9))
    assert next_debit_date(late_start, date(2026, 11, 2)) == date(2026, 12, 5)
    assert next_debit_date(replace(VIDEO, status=MandateStatus.SUSPENDED), date(2026, 11, 2)) == (
        date(2026, 11, 5)
    )
    for status in (MandateStatus.CANCELLED, MandateStatus.EXPIRED):
        assert next_debit_date(replace(VIDEO, status=status), date(2026, 11, 2)) is None


# Expected amount


def test_expected_amount_is_the_latest_paid_amount_else_the_agreed_one():
    assert expected_amount(VIDEO, []) == 2_500
    assert expected_amount(VIDEO, [request(S.AWAITING_CLIENT, 3_000)]) == 2_500
    paid = [request(S.PAID, 3_000), request(S.PAID, 2_800, at(12, 5, 9))]
    assert expected_amount(VIDEO, paid) == 2_800


# S5-ACCEPTANCE §3.1 and §3.2: no requests, no actions


def test_at_the_epoch_the_three_november_periods_are_protected():
    assert periods(VIDEO, EPOCH) == [("2026-11", 2_500)]
    assert periods(CLOUD, EPOCH) == [("2026-11", 2_100)]
    assert periods(FITNESS, EPOCH) == [("2026-11", 19_900)]
    assert total(EPOCH) == 24_500
    [video] = protected_periods(VIDEO, [], EPOCH)
    assert (video.due_date, video.until) == (date(2026, 11, 5), None)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (at(11, 4, 23, 59), 24_500),
        (at(11, 5), 27_000),
        (at(11, 6), 27_000),
        (at(11, 12), 29_100),
        (at(11, 20), 49_000),
        (at(11, 30, 23, 59), 49_000),
        (at(12, 1), 24_500),
    ],
)
def test_protection_over_time(now: datetime, expected: int):
    assert total(now) == expected


def test_the_next_period_joins_thirty_days_before_its_due_date():
    assert periods(VIDEO, at(11, 4, 23, 59)) == [("2026-11", 2_500)]
    assert periods(VIDEO, at(11, 5)) == [("2026-11", 2_500), ("2026-12", 2_500)]
    assert periods(VIDEO, at(12, 1)) == [("2026-12", 2_500)]


def test_a_suspended_agreement_keeps_its_scheduled_periods():
    suspended = replace(VIDEO, status=MandateStatus.SUSPENDED)
    assert periods(suspended, EPOCH) == periods(VIDEO, EPOCH)


def test_scheduled_periods_stay_within_the_term():
    ending = replace(VIDEO, expires_on=date(2026, 11, 5))
    assert periods(ending, at(11, 5)) == [("2026-11", 2_500)]
    assert periods(replace(VIDEO, expires_on=date(2026, 11, 4)), EPOCH) == []
    starting = replace(VIDEO, starts_on=date(2026, 11, 6), expires_on=date(2027, 11, 5))
    assert periods(starting, at(11, 5)) == [("2026-12", 2_500)]


def test_an_expired_agreement_keeps_only_declined_requests_until_their_deadline():
    expired = replace(VIDEO, status=MandateStatus.EXPIRED)
    assert periods(expired, EPOCH) == []
    declined = request(S.DECLINED)
    assert periods(expired, at(11, 5, 8, 59), declined) == [("2026-11", 2_500)]
    assert periods(expired, at(11, 5, 9), declined) == []


# Rule A and the expected amount


def test_a_paid_period_is_no_longer_protected_and_raises_the_next_expected_amount():
    paid = request(S.PAID, 3_000)
    assert periods(VIDEO, EPOCH, paid) == []
    assert periods(VIDEO, at(11, 5), paid) == [("2026-12", 3_000)]


def test_an_awaiting_request_protects_its_period_once():
    awaiting = request(S.AWAITING_CLIENT, 2_500)
    assert periods(VIDEO, EPOCH, awaiting) == [("2026-11", 2_500)]
    [period] = protected_periods(VIDEO, [awaiting], EPOCH)
    assert period.until is None


def test_out_of_mandate_and_auto_declined_requests_do_not_answer_the_period():
    others = (request(S.OUT_OF_MANDATE, 3_001), request(S.AUTO_DECLINED, 2_500))
    assert periods(VIDEO, EPOCH, *others) == [("2026-11", 2_500)]


# T111: an unanswered request, and the same after a decline


def test_t111_an_unanswered_request_protects_until_it_expires():
    awaiting = request(S.AWAITING_CLIENT)
    assert total(at(11, 5, 8, 59), video=[awaiting]) == 27_000
    expired = replace(awaiting, status=S.EXPIRED)
    assert total(at(11, 5, 9), video=[expired]) == 24_500
    later = request(S.AWAITING_CLIENT, received=at(11, 6, 9))
    assert total(at(11, 6, 9), video=[expired, later]) == 27_000
    assert periods(VIDEO, at(11, 6, 9), expired, later)[0] == ("2026-11", 2_500)


def test_t111_a_declined_request_protects_until_its_deadline():
    declined = request(S.DECLINED)
    assert total(at(11, 5, 8, 59), video=[declined]) == 27_000
    assert total(at(11, 5, 9), video=[declined]) == 24_500
    [november, _] = protected_periods(VIDEO, [declined], at(11, 5, 8, 59))
    assert november.until == at(11, 5, 9)
    # The declined request still occupies the period: a new one is PERIOD_USED.
    assert decide(now=at(11, 6, 9), period=(S.DECLINED,)) == (S.OUT_OF_MANDATE, R.PERIOD_USED)


# T101: fitness cancelled at the epoch


CANCELLED_FITNESS = replace(FITNESS, status=MandateStatus.CANCELLED, cancelled_at=EPOCH)


def test_t101_a_cancelled_agreement_protects_its_next_period_until_that_day_ends():
    cancelled = {"fitness": CANCELLED_FITNESS}
    assert total(EPOCH, agreements=cancelled) == 24_500
    assert total(at(11, 20, 23, 59), agreements=cancelled) == 29_100
    assert total(at(11, 21), agreements=cancelled) == 9_200
    [period] = protected_periods(CANCELLED_FITNESS, [], at(11, 20, 23, 59))
    assert (period.period_key, period.until) == ("2026-11", at(11, 21))


@pytest.mark.parametrize("now", [at(11, 20), at(11, 25), at(12, 1), at(12, 19), at(12, 20, 12)])
def test_t101_no_later_fitness_period_is_ever_protected(now: datetime):
    assert all(p.period_key == "2026-11" for p in protected_periods(CANCELLED_FITNESS, [], now))


def test_a_cancellation_after_the_due_date_protects_the_next_month():
    cancelled = replace(FITNESS, status=MandateStatus.CANCELLED, cancelled_at=at(11, 21, 10))
    assert periods(cancelled, at(11, 21, 10)) == [("2026-12", 19_900)]
    assert periods(cancelled, at(12, 21)) == []
    on_the_day = replace(FITNESS, status=MandateStatus.CANCELLED, cancelled_at=at(11, 20, 10))
    assert periods(on_the_day, at(11, 20, 10)) == [("2026-11", 19_900)]


def test_a_cancelled_period_already_paid_is_not_protected():
    paid = request(S.PAID, 19_900)
    cancelled = replace(FITNESS, status=MandateStatus.CANCELLED, cancelled_at=at(11, 3))
    assert periods(cancelled, at(11, 3), paid) == []


def test_a_declined_request_on_a_cancelled_agreement_keeps_the_later_end():
    declined = request(S.DECLINED, 19_900)
    cancelled = replace(FITNESS, status=MandateStatus.CANCELLED, cancelled_at=EPOCH)
    [period] = protected_periods(cancelled, [declined], EPOCH)
    assert period.until == at(11, 21)


def new_agreement(debit_day: int, today: datetime = EPOCH) -> MandateRow:
    """An agreement that starts on the day it is confirmed, as MANDATE_CREATE makes it."""
    return agreement(
        "mdt_music",
        1_500,
        2_000,
        debit_day,
        starts_on=today.date(),
        expires_on=date(2027, 11, 1),
    )


def test_a_new_agreement_protects_its_first_period_at_once():
    assert periods(new_agreement(15), EPOCH) == [("2026-11", 1_500)]


def test_a_new_agreement_whose_day_has_passed_protects_the_next_month():
    # Due 11-01 lies before the start, and 12-01 is within 30 days of 11-02.
    assert periods(new_agreement(1), EPOCH) == [("2026-12", 1_500)]


def test_a_new_agreement_due_today_protects_this_month_and_next():
    assert periods(new_agreement(2), EPOCH) == [("2026-11", 1_500), ("2026-12", 1_500)]


def test_a_new_agreement_protects_no_period_due_after_its_last_day():
    one_month = replace(new_agreement(15), expires_on=date(2026, 12, 1))
    assert periods(one_month, EPOCH) == [("2026-11", 1_500)]
    last_day_due = replace(new_agreement(1), expires_on=date(2026, 12, 1))
    assert periods(last_day_due, EPOCH) == [("2026-12", 1_500)]
    assert periods(replace(last_day_due, expires_on=date(2026, 11, 30)), EPOCH) == []
