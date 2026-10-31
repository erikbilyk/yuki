"""K3: tiers, the daily sum, forbidden limits, locks and read scopes (SPEC §10, §16).

The tier functions are pure; `daily_sum` and the read-scope registry read the database.
The gateway calls `transfer_tier` and `check_limits` again inside the submitting unit of
work, so a tier computed for a draft or a card is never trusted at execution time.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any

from wg.contracts.enums import ActionKind, AuditActor, LockScope, OperationStatus, Tier
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.tools import DataSource, Intent
from wg.k6 import audit, locks, mandates
from wg.k6.db import Database, Reader, in_transaction
from wg.k6.reserves import reserve_target
from wg.k6.simtime import SHANGHAI, to_db

# SPEC §10.1, in fen. The YELLOW limit is inclusive: a day total of exactly ¥1,000.00 is YELLOW.
YELLOW_DAILY_LIMIT_FEN = 100_000
SINGLE_TRANSFER_LIMIT_FEN = 5_000_000
DAILY_TRANSFER_LIMIT_FEN = 10_000_000

# Kinds whose amounts count toward the daily sum and its tiers (SPEC §10.2, S5-SPEC §5.9).
DAILY_SUM_KINDS: frozenset[ActionKind] = frozenset(
    {ActionKind.TRANSFER, ActionKind.PAY_BILL, ActionKind.DEBIT_PAY}
)
_COUNTED_STATUSES = (OperationStatus.SUBMITTED, OperationStatus.SETTLED, OperationStatus.UNKNOWN)

_FIXED_TIERS: dict[ActionKind, Tier] = {
    ActionKind.RESOLVE_ITEM: Tier.GREEN,
    ActionKind.CHECK_OUTCOME: Tier.GREEN,
    ActionKind.CONFIRM_BILL: Tier.YELLOW,
    ActionKind.ADD_PAYEE: Tier.RED,
    ActionKind.CANCEL_BILL: Tier.RED,
    ActionKind.MARK_PAID_EXTERNAL: Tier.RED,
    ActionKind.REDEEM: Tier.RED,
    ActionKind.PURCHASE: Tier.RED,
    # Direct-debit agreements (S5-SPEC §5.9): what lowers exposure is YELLOW, what widens it RED.
    ActionKind.DEBIT_DECLINE: Tier.YELLOW,
    ActionKind.MANDATE_CANCEL: Tier.YELLOW,
    ActionKind.MANDATE_SUSPEND: Tier.YELLOW,
    ActionKind.MANDATE_CAP_LOWER: Tier.YELLOW,
    ActionKind.MANDATE_RESUME: Tier.RED,
    ActionKind.MANDATE_CAP_RAISE: Tier.RED,
    ActionKind.MANDATE_CREATE: Tier.RED,
}

PLAN_APPROVAL_TIER = Tier.YELLOW
_ALWAYS_RISK_REDUCING = frozenset(
    {ActionKind.DEBIT_DECLINE, ActionKind.MANDATE_CANCEL, ActionKind.MANDATE_SUSPEND}
)

_TIER_ORDER = (Tier.GREEN, Tier.YELLOW, Tier.RED, Tier.FORBIDDEN)

EXTRA_READ_EVENT = "extra_read"


# Tiers (SPEC §10.1)


def max_tier(*tiers: Tier) -> Tier:
    """The strictest of the given tiers; a tier is the maximum over its factors."""
    return max(tiers, key=_TIER_ORDER.index)


def limit_refusal(amount_fen: int, day_sum_fen: int) -> ErrorCode | None:
    """The forbidden-limit code for a transfer of `amount_fen` on a day already at `day_sum_fen`."""
    if amount_fen > SINGLE_TRANSFER_LIMIT_FEN:
        return ErrorCode.SINGLE_LIMIT_EXCEEDED
    if day_sum_fen + amount_fen > DAILY_TRANSFER_LIMIT_FEN:
        return ErrorCode.DAILY_LIMIT_EXCEEDED
    return None


def transfer_tier_for(amount_fen: int, day_sum_fen: int) -> Tier:
    """Tier of a TRANSFER, PAY_BILL or DEBIT_PAY from the day total including this amount."""
    if amount_fen <= 0 or day_sum_fen < 0:
        raise ValueError("a transfer amount is positive and a day sum is not negative")
    if limit_refusal(amount_fen, day_sum_fen) is not None:
        return Tier.FORBIDDEN
    if day_sum_fen + amount_fen > YELLOW_DAILY_LIMIT_FEN:
        return Tier.RED
    return Tier.YELLOW


def reserve_tier_for(delta_fen: int) -> Tier:
    """Raising a reserve is YELLOW; lowering it is RED (protection falls, free money rises)."""
    return Tier.RED if delta_fen < 0 else Tier.YELLOW


def tier_for(
    kind: ActionKind,
    *,
    amount_fen: int | None = None,
    day_sum_fen: int = 0,
    reserve_delta_fen: int | None = None,
) -> Tier:
    """Tier of one action from the facts it depends on."""
    kind = ActionKind(kind)
    if kind in DAILY_SUM_KINDS:
        if amount_fen is None:
            raise ValueError(f"{kind} needs amount_fen")
        return transfer_tier_for(amount_fen, day_sum_fen)
    if kind is ActionKind.DESIGNATE_RESERVE:
        if reserve_delta_fen is None:
            raise ValueError("DESIGNATE_RESERVE needs reserve_delta_fen")
        return reserve_tier_for(reserve_delta_fen)
    return _FIXED_TIERS[kind]


# Daily sum (SPEC §10.2)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Start of `day` and of the next day in Asia/Shanghai."""
    start = datetime.combine(day, time(0, 0), tzinfo=SHANGHAI)
    return start, start + timedelta(days=1)


def daily_sum(reader: Reader, customer_id: str, day: date) -> int:
    """Σ amount_fen of the customer's operations of DAILY_SUM_KINDS submitted on `day`.

    Statuses SUBMITTED, SETTLED and UNKNOWN count, across all plans, each operation once.
    """
    start, end = day_bounds(day)
    kinds = sorted(k.value for k in DAILY_SUM_KINDS)
    statuses = [s.value for s in _COUNTED_STATUSES]
    row = reader.execute(
        "SELECT COALESCE(SUM(amount_fen), 0) FROM operations"
        f" WHERE customer_id = ? AND kind IN ({_marks(kinds)}) AND status IN ({_marks(statuses)})"
        " AND submitted_at >= ? AND submitted_at < ?",
        (customer_id, *kinds, *statuses, to_db(start), to_db(end)),
    ).fetchone()
    return int(row[0])


def transfer_tier(reader: Reader, customer_id: str, amount_fen: int, at: datetime) -> Tier:
    """Tier of a new transfer of `amount_fen` by this customer at simulated time `at`."""
    day = at.astimezone(SHANGHAI).date()
    return transfer_tier_for(amount_fen, daily_sum(reader, customer_id, day))


def check_limits(reader: Reader, customer_id: str, amount_fen: int, at: datetime) -> None:
    """Refuse a transfer above the single or the daily forbidden limit."""
    day = at.astimezone(SHANGHAI).date()
    code = limit_refusal(amount_fen, daily_sum(reader, customer_id, day))
    if code is not None:
        raise WgError(code, {"amount_fen": amount_fen})


def _marks(values: list[str]) -> str:
    return ", ".join("?" for _ in values)


# Locks (SPEC §16)


def is_risk_reducing(
    reader: Reader, customer_id: str, kind: ActionKind, params: Mapping[str, Any]
) -> bool:
    """True for a write that only raises protection (SPEC §16, S5-SPEC §5.9).

    A reserve increase; declining a merchant's request (no money moves and the period stays
    protected until the request's deadline); closing or suspending an agreement, or lowering
    its cap below the current one (merchants can then take less, never more).
    """
    kind = ActionKind(kind)
    if kind in _ALWAYS_RISK_REDUCING:
        return True
    if kind is ActionKind.MANDATE_CAP_LOWER:
        mandate = mandates.find_mandate(reader, str(params["mandate_ref"]))
        return mandate is not None and int(params["cap_fen"]) < mandate.cap_fen
    if kind is not ActionKind.DESIGNATE_RESERVE:
        return False
    return int(params["target_fen"]) >= reserve_target(reader, customer_id)


def check_not_locked(
    reader: Reader, customer_id: str, plan_id: str | None, *, risk_reducing: bool = False
) -> None:
    """Refuse a new write under an active lock in scope, unless it reduces risk."""
    if risk_reducing:
        return
    active = locks.locks_in_scope(reader, customer_id, plan_id)
    if active:
        lock = active[0]
        raise WgError(
            ErrorCode.LOCKED_WRITE,
            {"lock_id": lock.lock_id, "scope": LockScope(lock.scope).value},
        )


# Read scopes (SPEC §10.3)


def allowed_sources(reader: Reader, intent: Intent) -> frozenset[DataSource]:
    """The sources a call under `intent` may read, from the `read_scopes` table."""
    row = reader.execute(
        "SELECT allowed_sources FROM read_scopes WHERE intent = ?", (Intent(intent).value,)
    ).fetchone()
    if row is None:
        return frozenset()
    return frozenset(DataSource(source) for source in json.loads(row["allowed_sources"]))


def extra_sources(
    reader: Reader, intent: Intent, sources: frozenset[DataSource]
) -> frozenset[DataSource]:
    """The sources outside the declared intent's scope; empty when the call is in scope."""
    return frozenset(sources) - allowed_sources(reader, intent)


def check_read_scope(
    db: Database,
    *,
    customer_id: str,
    tool: str,
    intent: Intent,
    sources: frozenset[DataSource],
) -> None:
    """Refuse a read outside the declared intent and count it as an extra read in the audit.

    This is data minimization and a metric; ownership is the authorization boundary. The
    audit row is written in its own unit, so it is never part of the caller's work.
    """
    if in_transaction():
        raise RuntimeError("check_read_scope runs before the caller opens a unit of work")
    with db.read() as conn:
        extra = extra_sources(conn, intent, sources)
    if not extra:
        return
    names = ",".join(sorted(source.value for source in extra))
    db.run_in_uow(
        lambda uow: audit.record(
            uow,
            AuditActor.AGENT,
            EXTRA_READ_EVENT,
            customer_id=customer_id,
            ref=tool,
            decision=ErrorCode.READ_SCOPE_DENIED.value,
            reason=f"{Intent(intent).value}:{names}"[:64],
        ),
        "extra read",
    )
    raise WgError(
        ErrorCode.READ_SCOPE_DENIED,
        {"tool": tool, "intent": Intent(intent).value, "sources": sorted(s.value for s in extra)},
    )


def extra_read_count(reader: Reader, customer_id: str | None = None) -> int:
    """The extra-read metric: refused out-of-scope reads recorded in the audit log."""
    if customer_id is None:
        row = reader.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event = ?", (EXTRA_READ_EVENT,)
        ).fetchone()
    else:
        row = reader.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event = ? AND customer_id = ?",
            (EXTRA_READ_EVENT, customer_id),
        ).fetchone()
    return int(row[0])
