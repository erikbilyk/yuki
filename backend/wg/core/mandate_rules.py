"""Rules of direct-debit agreements (S5-SPEC §5.1–§5.3, §5.6); pure functions, no database.

Periods are calendar months in Asia/Shanghai. An agreement authorizes debits until the
start of the day after `expires_on`; a merchant's request can be answered for 72 hours.
Protected periods are derived from the agreement, its requests and the clock; they are
passed to the cash engine as commitments and never stored. What a change of an agreement
does to its pending requests is decided here; K6 writes it.
"""

from __future__ import annotations

import calendar
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol

from wg.contracts.enums import ActionKind, DebitReason, DebitRequestStatus, MandateStatus
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

# A scheduled period without one of these requests is still protected (rule B).
_ANSWERED_STATUSES = OCCUPYING_STATUSES | {DebitRequestStatus.EXPIRED}
HORIZON = timedelta(days=30)
SUBSCRIPTION_PREFIX = "cmt_sub_"
_MANDATE_PREFIX = "mdt_"

type RequestOutcome = tuple[DebitRequestStatus, DebitReason | None]


class RequestState(Protocol):
    """What protection needs of a stored non-probe request."""

    @property
    def period_key(self) -> str: ...
    @property
    def status(self) -> DebitRequestStatus: ...
    @property
    def amount_fen(self) -> int: ...
    @property
    def respond_by(self) -> datetime: ...


class PendingRequest(RequestState, Protocol):
    """What an agreement change needs of a stored non-probe request."""

    @property
    def request_id(self) -> str: ...
    @property
    def received_at(self) -> datetime: ...
    @property
    def reason_code(self) -> DebitReason | None: ...


@dataclass(frozen=True)
class RequestChange:
    """A request's new status after a change or the expiry of its agreement."""

    request_id: str
    status: DebitRequestStatus
    reason: DebitReason | None


@dataclass(frozen=True)
class ProtectedPeriod:
    """A period whose expected debit is kept out of free money; `until` is its end if known."""

    period_key: str
    due_date: date
    expected_fen: int
    until: datetime | None


def period_key(moment: datetime) -> str:
    """The CST calendar month of `moment`, such as "2026-11"."""
    local = _local(moment)
    return f"{local.year:04d}-{local.month:02d}"


def _month(key: str) -> tuple[int, int]:
    year, month = key.split("-")
    return int(year), int(month)


def _key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def next_period(key: str) -> str:
    year, month = _month(key)
    return _key(year + 1, 1) if month == 12 else _key(year, month + 1)


def _day_key(day: date) -> str:
    return _key(day.year, day.month)


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=SHANGHAI)


def due_date(debit_day: int, key: str) -> date:
    """The debit date of period `key`: `debit_day`, or the month's last day if it is shorter."""
    if not 1 <= debit_day <= 31:
        raise ValueError(f"debit day out of range: {debit_day}")
    year, month = _month(key)
    return date(year, month, min(debit_day, calendar.monthrange(year, month)[1]))


def expiry_instant(expires_on: date) -> datetime:
    """When an agreement stops authorizing: 00:00 CST on the day after `expires_on`."""
    return _start_of(expires_on + timedelta(days=1))


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


def next_debit_date(mandate: MandateRow, today: date) -> date | None:
    """The next due date on or after today (and the start) while the agreement can be debited.

    None once it is cancelled or expired, or when the next due date lies after `expires_on`.
    """
    if mandate.status not in (MandateStatus.ACTIVE, MandateStatus.SUSPENDED):
        return None
    start = max(today, mandate.starts_on)
    due = due_date(mandate.debit_day, _first_period_after(mandate.debit_day, start))
    return due if due <= mandate.expires_on else None


# Agreement changes (S5-SPEC §5.3, §6.1)

_LIVE = frozenset({MandateStatus.ACTIVE, MandateStatus.SUSPENDED})
# The statuses each change starts from.
CHANGE_FROM: dict[ActionKind, frozenset[MandateStatus]] = {
    ActionKind.MANDATE_CANCEL: _LIVE,
    ActionKind.MANDATE_SUSPEND: frozenset({MandateStatus.ACTIVE}),
    ActionKind.MANDATE_RESUME: frozenset({MandateStatus.SUSPENDED}),
    ActionKind.MANDATE_CAP_LOWER: _LIVE,
    ActionKind.MANDATE_CAP_RAISE: _LIVE,
}
CHANGE_KINDS = frozenset(CHANGE_FROM)
CAP_KINDS = frozenset({ActionKind.MANDATE_CAP_LOWER, ActionKind.MANDATE_CAP_RAISE})
# The status a status change leads to.
CHANGE_TO: dict[ActionKind, MandateStatus] = {
    ActionKind.MANDATE_CANCEL: MandateStatus.CANCELLED,
    ActionKind.MANDATE_SUSPEND: MandateStatus.SUSPENDED,
    ActionKind.MANDATE_RESUME: MandateStatus.ACTIVE,
}


def cap_change_kind(current_cap_fen: int, cap_fen: int) -> ActionKind | None:
    """MANDATE_CAP_LOWER or MANDATE_CAP_RAISE by direction; None when the cap stays the same."""
    if cap_fen < current_cap_fen:
        return ActionKind.MANDATE_CAP_LOWER
    if cap_fen > current_cap_fen:
        return ActionKind.MANDATE_CAP_RAISE
    return None


def request_changes(
    kind: ActionKind,
    mandate: MandateRow,
    requests: Sequence[PendingRequest],
    now: datetime,
    *,
    cap_fen: int | None = None,
) -> tuple[RequestChange, ...]:
    """What a change of `mandate` (as it is before the change) does to its requests.

    `requests` are the agreement's non-probe requests. Cancel and suspend decline the
    requests awaiting the customer; a lower cap moves those above it out of the agreement;
    a higher cap re-checks the CAP requests (`cap_raise_changes`); resume changes none.
    """
    kind = ActionKind(kind)
    if kind in (ActionKind.MANDATE_CANCEL, ActionKind.MANDATE_SUSPEND):
        reason = _INACTIVE_REASON[CHANGE_TO[kind]]
        return tuple(
            RequestChange(r.request_id, DebitRequestStatus.AUTO_DECLINED, reason)
            for r in requests
            if r.status is DebitRequestStatus.AWAITING_CLIENT
        )
    if kind is ActionKind.MANDATE_CAP_LOWER:
        assert cap_fen is not None
        return tuple(
            RequestChange(r.request_id, DebitRequestStatus.OUT_OF_MANDATE, DebitReason.CAP)
            for r in requests
            if r.status is DebitRequestStatus.AWAITING_CLIENT and r.amount_fen > cap_fen
        )
    if kind is ActionKind.MANDATE_CAP_RAISE:
        assert cap_fen is not None
        return cap_raise_changes(mandate, requests, cap_fen, now)
    return ()


def cap_raise_changes(
    mandate: MandateRow, requests: Sequence[PendingRequest], cap_fen: int, now: datetime
) -> tuple[RequestChange, ...]:
    """The re-check of CAP requests when the cap rises to `cap_fen` (S5-SPEC §5.3, C-14).

    Per period, the CAP requests still answerable and within the new cap are eligible, and
    only while the agreement is ACTIVE and unexpired. In a period without a request awaiting
    the customer, paid or declined, the earliest eligible one by (received_at, request_id)
    awaits the customer again and the other eligible ones become PERIOD_USED; in an occupied
    period every eligible one becomes PERIOD_USED. Every other request is untouched.
    """
    if mandate.status is not MandateStatus.ACTIVE or not authorizes(mandate.expires_on, now):
        return ()
    eligible: dict[str, list[PendingRequest]] = {}
    for request in requests:
        if (
            request.status is DebitRequestStatus.OUT_OF_MANDATE
            and request.reason_code is DebitReason.CAP
            and now < request.respond_by
            and request.amount_fen <= cap_fen
        ):
            eligible.setdefault(request.period_key, []).append(request)
    occupied = {r.period_key for r in requests if r.status in OCCUPYING_STATUSES}
    changes: list[RequestChange] = []
    for key in sorted(eligible):
        ordered = sorted(eligible[key], key=lambda r: (r.received_at, r.request_id))
        if key not in occupied:
            changes.append(
                RequestChange(ordered[0].request_id, DebitRequestStatus.AWAITING_CLIENT, None)
            )
            ordered = ordered[1:]
        changes.extend(
            RequestChange(r.request_id, DebitRequestStatus.OUT_OF_MANDATE, DebitReason.PERIOD_USED)
            for r in ordered
        )
    return tuple(changes)


def expiry_changes(requests: Sequence[PendingRequest]) -> tuple[RequestChange, ...]:
    """When an agreement expires, the requests awaiting the customer are declined."""
    return tuple(
        RequestChange(r.request_id, DebitRequestStatus.AUTO_DECLINED, DebitReason.MANDATE_EXPIRED)
        for r in requests
        if r.status is DebitRequestStatus.AWAITING_CLIENT
    )


# Protection (S5-SPEC §5.6)


def expected_amount(mandate: MandateRow, requests: Sequence[RequestState]) -> int:
    """The amount a period is expected to cost: the latest paid request's, else the agreed one.

    `requests` are the agreement's non-probe requests in arrival order.
    """
    paid = [r.amount_fen for r in requests if r.status is DebitRequestStatus.PAID]
    return paid[-1] if paid else mandate.expected_fen


def protected_periods(
    mandate: MandateRow, requests: Sequence[RequestState], now: datetime
) -> tuple[ProtectedPeriod, ...]:
    """The agreement's protected periods at `now`, each once, in period order.

    A: the period of each request awaiting the customer, and of each declined request until
    its `respond_by`. B: while ACTIVE or SUSPENDED, this month's and next month's periods
    due within the term and the next 30 days that have no request awaiting, paid, declined
    or expired. C: once CANCELLED, the period of the first due date on or after the
    cancellation, until the end of that day, unless it was paid. EXPIRED agreements keep
    only A.
    """
    today = _local(now).date()
    expected = expected_amount(mandate, requests)
    found: dict[str, datetime | None] = {}

    def protect(key: str, until: datetime | None) -> None:
        if key in found:
            earlier = found[key]
            until = None if earlier is None or until is None else max(earlier, until)
        found[key] = until

    for request in requests:
        if request.status is DebitRequestStatus.AWAITING_CLIENT:
            protect(request.period_key, None)
        elif request.status is DebitRequestStatus.DECLINED and now < request.respond_by:
            protect(request.period_key, request.respond_by)

    if mandate.status in (MandateStatus.ACTIVE, MandateStatus.SUSPENDED):
        answered = {r.period_key for r in requests if r.status in _ANSWERED_STATUSES}
        this_month = _day_key(today)
        for key in (this_month, next_period(this_month)):
            if key not in answered and _schedulable(mandate, key, today):
                protect(key, None)
    elif mandate.status is MandateStatus.CANCELLED and mandate.cancelled_at is not None:
        key = _first_period_after(mandate.debit_day, _local(mandate.cancelled_at).date())
        until = _start_of(due_date(mandate.debit_day, key) + timedelta(days=1))
        paid = {r.period_key for r in requests if r.status is DebitRequestStatus.PAID}
        if now < until and key not in paid and _schedulable(mandate, key, today):
            protect(key, until)

    return tuple(
        ProtectedPeriod(
            period_key=key,
            due_date=due_date(mandate.debit_day, key),
            expected_fen=expected,
            until=found[key],
        )
        for key in sorted(found)
    )


def _schedulable(mandate: MandateRow, key: str, today: date) -> bool:
    """The period's due date lies within the agreement's term and the 30-day horizon."""
    due = due_date(mandate.debit_day, key)
    return mandate.starts_on <= due <= mandate.expires_on and due <= today + HORIZON


def _first_period_after(debit_day: int, day: date) -> str:
    """The period of the first due date on or after `day`."""
    key = _day_key(day)
    return key if due_date(debit_day, key) >= day else next_period(key)


def subscription_commitment_id(mandate_ref: str, key: str) -> str:
    """The cash engine's id for a protected period: `cmt_sub_<ref suffix>_<YYYYMM>`."""
    return (
        f"{SUBSCRIPTION_PREFIX}{mandate_ref.removeprefix(_MANDATE_PREFIX)}_{key.replace('-', '')}"
    )


def parse_subscription_commitment_id(commitment_id: str) -> tuple[str, str] | None:
    """(mandate_ref, period_key) of a protected period's id, None for a stored commitment.

    Read from the right: the last segment is the month, the rest is the ref's suffix, which
    may itself contain underscores.
    """
    if not commitment_id.startswith(SUBSCRIPTION_PREFIX):
        return None
    suffix, _, month = commitment_id.removeprefix(SUBSCRIPTION_PREFIX).rpartition("_")
    if not suffix or len(month) != 6 or not month.isdigit():
        return None
    return _MANDATE_PREFIX + suffix, f"{month[:4]}-{month[4:]}"
