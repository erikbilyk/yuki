"""Cash engine: C, E, L, B, M, U, headroom, shortfall and the bill walk (SPEC §13)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from wg.contracts.engines import (
    Assumption,
    AssumptionKind,
    BillState,
    CashInputs,
    CashSnapshotValues,
    CommitmentInput,
    SnapshotQuality,
)
from wg.contracts.enums import BillStatus, CommitmentStatus

SHANGHAI = ZoneInfo("Asia/Shanghai")
UNPAID_STATUSES = frozenset({CommitmentStatus.CONFIRMED, CommitmentStatus.PARTIALLY_PAID})


def compute_snapshot(inputs: CashInputs) -> CashSnapshotValues:
    """Compute the amounts of one cash snapshot from its inputs."""
    today = inputs.as_of.astimezone(SHANGHAI).date()
    covered = [account for account in inputs.accounts if account.covered]
    c_fen = sum(account.posted_fen for account in covered)

    unpaid = _by_due_date(c for c in inputs.commitments if c.status in UNPAID_STATUSES)
    horizon_end = today + timedelta(days=inputs.horizon_days)
    in_horizon = [c for c in unpaid if c.due_date <= horizon_end]
    b_fen = sum(c.unpaid_fen for c in in_horizon)

    # A hold that shares a purpose with a bill counted in B is that bill's payment.
    paired = {c.purpose_id for c in in_horizon}
    u_fen = sum(h.amount_fen for h in inputs.holds if h.purpose_id not in paired)

    e_fen = inputs.reserve_fen
    l_fen = inputs.allowance_remaining_fen
    m_fen = inputs.margin_fen
    protected_fen = e_fen + l_fen + b_fen + m_fen

    return CashSnapshotValues(
        c_fen=c_fen,
        e_fen=e_fen,
        l_fen=l_fen,
        b_fen=b_fen,
        m_fen=m_fen,
        u_fen=u_fen,
        protected_fen=protected_fen,
        headroom_fen=max(0, c_fen - protected_fen - u_fen),
        shortfall_fen=max(0, protected_fen + u_fen - c_fen),
        bills=_bill_states(
            inputs.commitments, unpaid, today, c_fen - e_fen - l_fen - m_fen - u_fen
        ),
        monthly_estimate_fen=_monthly_estimate(inputs, unpaid, today),
        quality=_quality(inputs),
        inputs_hash=_inputs_hash(inputs),
    )


def _by_due_date(commitments: Iterable[CommitmentInput]) -> list[CommitmentInput]:
    return sorted(commitments, key=lambda c: (c.due_date, c.commitment_id))


def _bill_states(
    commitments: tuple[CommitmentInput, ...],
    unpaid: list[CommitmentInput],
    today: date,
    free_fen: int,
) -> tuple[BillState, ...]:
    """Walk the unpaid bills by due date; a bill is secured while the running total fits."""
    states: list[BillState] = []
    running_fen = 0
    for bill in unpaid:
        running_fen += bill.unpaid_fen
        if bill.due_date < today:
            status = BillStatus.OVERDUE
        elif running_fen <= free_fen:
            status = BillStatus.SECURED
        else:
            status = BillStatus.UNSECURED
        states.append(_state(bill, bill.unpaid_fen, status))
    settled = {
        CommitmentStatus.PAID: BillStatus.PAID,
        CommitmentStatus.PAID_REPORTED: BillStatus.PAID_REPORTED,
    }
    for bill in commitments:
        if bill.status in settled:
            states.append(_state(bill, 0, settled[bill.status]))
    return tuple(sorted(states, key=lambda s: (s.due_date, s.commitment_id)))


def _state(bill: CommitmentInput, unpaid_fen: int, status: BillStatus) -> BillState:
    return BillState(
        commitment_id=bill.commitment_id,
        unpaid_fen=unpaid_fen,
        due_date=bill.due_date,
        status=status,
    )


def _monthly_estimate(inputs: CashInputs, unpaid: list[CommitmentInput], today: date) -> int:
    """Income so far, minus spending so far, the allowance left and bills due this month."""
    due_this_month = sum(
        c.unpaid_fen
        for c in unpaid
        if (c.due_date.year, c.due_date.month) == (today.year, today.month)
    )
    return (
        inputs.income_this_month_fen
        - inputs.spent_this_month_fen
        - inputs.allowance_remaining_fen
        - due_this_month
    )


def _quality(inputs: CashInputs) -> SnapshotQuality:
    reconciled = {
        a.account_id: a.opening_fen + a.imported_sum_fen == a.closing_fen for a in inputs.accounts
    }
    return SnapshotQuality(
        as_of=inputs.as_of,
        uncovered_accounts=tuple(a.account_id for a in inputs.accounts if not a.covered),
        unresolved_count=len(inputs.unresolved),
        unresolved_impact_fen=sum(abs(item.amount_fen) for item in inputs.unresolved),
        reconciled=reconciled,
        assumptions=_assumptions(inputs),
        incomplete=any(a.covered and not reconciled[a.account_id] for a in inputs.accounts),
    )


def _assumptions(inputs: CashInputs) -> tuple[Assumption, ...]:
    """Supplied assumptions plus one for each bill taken as paid on the customer's word."""
    result = list(inputs.assumptions)
    for bill in _by_due_date(
        c for c in inputs.commitments if c.status is CommitmentStatus.PAID_REPORTED
    ):
        reported = Assumption(kind=AssumptionKind.PAID_REPORTED, ref=bill.commitment_id)
        if reported not in result:
            result.append(reported)
    return tuple(result)


def _inputs_hash(inputs: CashInputs) -> str:
    canonical = json.dumps(
        inputs.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
