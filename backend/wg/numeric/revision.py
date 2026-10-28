"""Purchase amount after new bills, within the bounds of the plan approval (SPEC §14.6, C-09)."""

from __future__ import annotations

from wg.contracts.engines import PlanBounds, RevisionDecision, RevisionOutcome
from wg.contracts.errors import ErrorCode


def decide_purchase(
    x_fen: int, bounds: PlanBounds | None, current_fen: int, headroom_fen: int
) -> RevisionDecision:
    """Decide the purchase after bills that add `x_fen` in total since the approval.

    With bounds the purchase is P(x) = p0 − max(0, x − spare0), and every bound is checked
    against the approval's baseline, never against the last revision. Without bounds the
    approved order `current_fen` is exact: it either still fits the headroom or pauses.
    """
    if x_fen < 0:
        raise ValueError("bill increase must not be negative")
    reasons: list[ErrorCode] = []
    if bounds is None:
        purchase = current_fen
        if purchase > headroom_fen:
            reasons.append(ErrorCode.EXACT_ORDERS_ONLY)
    else:
        purchase = bounds.p0_fen - max(0, x_fen - bounds.spare0_fen)
        if purchase != bounds.p0_fen:
            reasons.extend(_bound_reasons(x_fen, purchase, bounds))
        if purchase > headroom_fen:
            reasons.append(ErrorCode.INSUFFICIENT_HEADROOM)

    if reasons:
        outcome = RevisionOutcome.PAUSE
    elif purchase == current_fen:
        outcome = RevisionOutcome.UNCHANGED
    else:
        outcome = RevisionOutcome.REVISE
    return RevisionDecision(
        outcome=outcome, x_fen=x_fen, purchase_fen=purchase, reasons=tuple(reasons)
    )


def _bound_reasons(x_fen: int, purchase_fen: int, bounds: PlanBounds) -> list[ErrorCode]:
    reasons = []
    if x_fen > bounds.max_added_fen:
        reasons.append(ErrorCode.BILL_INCREASE_OVER_LIMIT)
    if bounds.p0_fen - purchase_fen > bounds.max_retained_fen:
        reasons.append(ErrorCode.RETENTION_OVER_LIMIT)
    if purchase_fen < bounds.min_purchase_fen:
        reasons.append(ErrorCode.PURCHASE_BELOW_MIN)
    return reasons
