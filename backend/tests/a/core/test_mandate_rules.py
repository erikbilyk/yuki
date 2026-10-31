"""Periods, due dates, expiry and answer deadlines of direct-debit agreements (S5-SPEC §5.1)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from wg.core.mandate_rules import (
    authorizes,
    due_date,
    expiry_instant,
    period_key,
    respond_by,
    term_end,
)
from wg.k6.simtime import SHANGHAI


def cst(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=SHANGHAI)


def test_period_is_the_cst_calendar_month():
    assert period_key(cst(2026, 11, 2, 9, 0)) == "2026-11"
    assert period_key(cst(2026, 11, 30, 23, 59)) == "2026-11"
    assert period_key(cst(2026, 12, 1, 0, 1)) == "2026-12"
    assert period_key(cst(2026, 12, 31, 23, 59, 59)) == "2026-12"
    assert period_key(cst(2027, 1, 1, 0, 0)) == "2027-01"
    # 16:30 UTC on Nov 30 is already 00:30 on Dec 1 in Shanghai.
    assert period_key(datetime(2026, 11, 30, 16, 30, tzinfo=UTC)) == "2026-12"
    with pytest.raises(ValueError):
        period_key(datetime(2026, 11, 2, 9, 0))


@pytest.mark.parametrize(
    ("debit_day", "key", "expected"),
    [
        (5, "2026-11", date(2026, 11, 5)),
        (31, "2026-11", date(2026, 11, 30)),
        (31, "2026-12", date(2026, 12, 31)),
        (31, "2027-02", date(2027, 2, 28)),
        (30, "2027-02", date(2027, 2, 28)),
        (29, "2028-02", date(2028, 2, 29)),
        (31, "2028-02", date(2028, 2, 29)),
        (28, "2027-02", date(2027, 2, 28)),
        (1, "2027-01", date(2027, 1, 1)),
    ],
)
def test_due_date_falls_back_to_the_last_day_of_a_short_month(debit_day, key, expected):
    assert due_date(debit_day, key) == expected


@pytest.mark.parametrize("debit_day", [0, 32])
def test_due_date_refuses_a_day_outside_1_to_31(debit_day):
    with pytest.raises(ValueError):
        due_date(debit_day, "2026-11")


def test_an_agreement_authorizes_until_midnight_after_its_last_day():
    expires_on = date(2026, 11, 5)
    assert expiry_instant(expires_on) == cst(2026, 11, 6, 0, 0)
    assert authorizes(expires_on, cst(2026, 11, 5, 23, 59, 59))
    assert not authorizes(expires_on, cst(2026, 11, 6, 0, 0))
    assert expiry_instant(date(2026, 12, 31)) == cst(2027, 1, 1, 0, 0)
    assert expiry_instant(date(2028, 2, 28)) == cst(2028, 2, 29, 0, 0)


def test_respond_by_is_72_hours_after_receipt():
    received = cst(2026, 11, 30, 23, 59)
    assert respond_by(received) == cst(2026, 12, 3, 23, 59)
    assert respond_by(datetime(2026, 11, 2, 1, 0, tzinfo=UTC)) - timedelta(hours=72) == cst(
        2026, 11, 2, 9, 0
    )
    assert respond_by(received).tzinfo is SHANGHAI


@pytest.mark.parametrize(
    ("starts_on", "months", "expected"),
    [
        (date(2026, 11, 2), 12, date(2027, 11, 1)),
        (date(2026, 11, 2), 1, date(2026, 12, 1)),
        (date(2026, 12, 15), 1, date(2027, 1, 14)),
        (date(2026, 1, 31), 1, date(2026, 2, 27)),
        (date(2026, 8, 31), 6, date(2027, 2, 27)),
        (date(2028, 2, 29), 12, date(2029, 2, 27)),
        (date(2026, 3, 1), 12, date(2027, 2, 28)),
    ],
)
def test_a_term_ends_the_day_before_the_same_day_months_later(starts_on, months, expected):
    assert term_end(starts_on, months) == expected


@pytest.mark.parametrize("months", [0, -1, 13, 24])
def test_a_term_outside_1_to_12_months_is_refused(months):
    with pytest.raises(ValueError):
        term_end(date(2026, 11, 2), months)


@pytest.mark.parametrize("starts_on", [date(2026, 11, 2), date(2028, 2, 29), date(2026, 12, 31)])
def test_a_twelve_month_term_stays_within_the_stored_limit(starts_on):
    # The schema accepts expires_on up to starts_on + 12 months (S5-SPEC §4.2).
    limit = date(starts_on.year + 1, starts_on.month, 28) + timedelta(days=4)
    assert starts_on < term_end(starts_on, 12) <= limit
