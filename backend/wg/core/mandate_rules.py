"""Rules of direct-debit agreements (S5-SPEC §5.1, §5.2); pure functions, no database.

Periods are calendar months in Asia/Shanghai. An agreement authorizes debits until the
start of the day after `expires_on`; a merchant's request can be answered for 72 hours.
"""

from __future__ import annotations

import calendar
from collections.abc import Collection
from datetime import date, datetime, time, timedelta

from wg.contracts.enums import DebitReason, DebitRequestStatus, MandateStatus
from wg.k6.mandates import MandateRow
from wg.k6.simtime import SHANGHAI

RESPOND_WINDOW = timedelta(hours=72)
# A period holds at most one request that awaits the customer, is paid or is declined.
OCCUPYING_STATUSES = frozenset(
    {DebitRequestStatus.AWAITING_CLIENT, DebitRequestStatus.PAID, DebitRequestStatus.DECLINED}
)
# Stored requests of one agreement in one period, whatever their status, before the period
# takes no more.
REQUESTS_PER_PERIOD = 3
_INACTIVE_REASON = {
    MandateStatus.SUSPENDED: DebitReason.MANDATE_SUSPENDED,
    MandateStatus.CANCELLED: DebitReason.MANDATE_CANCELLED,
    MandateStatus.EXPIRED: DebitReason.MANDATE_EXPIRED,
}

type RequestOutcome = tuple[DebitRequestStatus, DebitReason | None]


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


def format_problem(amount_fen: int, currency: str) -> str | None:
    """`amount` or `currency` when a merchant's request is malformed, else None."""
    if amount_fen <= 0:
        return "amount"
    if currency != "CNY":
        return "currency"
    return None


def decide_request(
    mandate: MandateRow | None,
    *,
    merchant_verified: bool,
    merchant_id: str,
    amount_fen: int,
    now: datetime,
    period_statuses: Collection[DebitRequestStatus],
    probe: bool = False,
) -> RequestOutcome:
    """The outcome of a well-formed request; the first failing check decides (S5-SPEC §5.2).

    `mandate` is the agreement the request names, if any; `period_statuses` are the statuses
    of the agreement's stored non-probe requests in the period of `now`. The cap is checked
    before the period, so a request above the cap is CAP even in a used period. Probes never
    use or count against a period.
    """
    if not merchant_verified or mandate is None or mandate.merchant_id != merchant_id:
        return DebitRequestStatus.AUTO_DECLINED, DebitReason.NO_MANDATE
    if mandate.status is not MandateStatus.ACTIVE:
        return DebitRequestStatus.AUTO_DECLINED, _INACTIVE_REASON[mandate.status]
    if not authorizes(mandate.expires_on, now):
        return DebitRequestStatus.AUTO_DECLINED, DebitReason.MANDATE_EXPIRED
    if amount_fen > mandate.cap_fen:
        return DebitRequestStatus.OUT_OF_MANDATE, DebitReason.CAP
    if not probe and (
        OCCUPYING_STATUSES.intersection(period_statuses)
        or len(period_statuses) >= REQUESTS_PER_PERIOD
    ):
        return DebitRequestStatus.OUT_OF_MANDATE, DebitReason.PERIOD_USED
    return DebitRequestStatus.AWAITING_CLIENT, None
