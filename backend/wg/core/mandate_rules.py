"""Time rules of direct-debit agreements (S5-SPEC §5.1); pure functions, no database.

Periods are calendar months in Asia/Shanghai. An agreement authorizes debits until the
start of the day after `expires_on`; a merchant's request can be answered for 72 hours.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta

from wg.k6.simtime import SHANGHAI

RESPOND_WINDOW = timedelta(hours=72)


def period_key(moment: datetime) -> str:
    """The CST calendar month of `moment`, such as "2026-11"."""
    local = _local(moment)
    return f"{local.year:04d}-{local.month:02d}"


def _month(key: str) -> tuple[int, int]:
    year, month = key.split("-")
    return int(year), int(month)


def due_date(debit_day: int, key: str) -> date:
    """The debit date of period `key`: `debit_day`, or the month's last day if it is shorter."""
    if not 1 <= debit_day <= 31:
        raise ValueError(f"debit day out of range: {debit_day}")
    year, month = _month(key)
    return date(year, month, min(debit_day, calendar.monthrange(year, month)[1]))


def expiry_instant(expires_on: date) -> datetime:
    """When an agreement stops authorizing: 00:00 CST on the day after `expires_on`."""
    return datetime.combine(expires_on + timedelta(days=1), time(), tzinfo=SHANGHAI)


def authorizes(expires_on: date, now: datetime) -> bool:
    """Authority is decided by the clock, never by the stored status alone."""
    return now < expiry_instant(expires_on)


def respond_by(received_at: datetime) -> datetime:
    """The deadline for the customer's answer to a request received at `received_at`."""
    return _local(received_at) + RESPOND_WINDOW


def _local(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetime")
    return moment.astimezone(SHANGHAI)
