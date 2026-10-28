"""Redemption fees over FIFO lots with flat or tiered schedules (SPEC §14.1, §14.4)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from wg.contracts.engines import FeeQuote, FeeSchedule, FeeStatus, Lot, LotDraw
from wg.contracts.money import quantize_fen


def redemption_fee(
    lots: Sequence[Lot], shares: Decimal, nav: Decimal, schedule: FeeSchedule, as_of: date
) -> FeeQuote:
    """Gross, fee and net of redeeming `shares` of one fund at `nav`.

    Lots are drawn oldest first. The quote is `unverified` when the lots do not cover the
    shares or a lot's holding period has no tier; it then carries no fee.
    """
    if shares <= 0:
        raise ValueError("shares to redeem must be positive")
    if len({lot.product_id for lot in lots}) > 1:
        raise ValueError("lots of more than one fund")

    gross_fen = quantize_fen(shares * nav)
    draws = _draw_fifo(lots, shares, schedule, as_of)
    if draws is None:
        return FeeQuote(
            shares=shares,
            gross_fen=gross_fen,
            fee_fen=None,
            net_fen=None,
            status=FeeStatus.UNVERIFIED,
            terms_version=schedule.terms_version,
            draws=(),
        )
    # One rounding for the whole fee, from the exact gross of each draw.
    fee_fen = quantize_fen(sum((d.shares * nav * d.rate for d in draws), Decimal(0)))
    return FeeQuote(
        shares=shares,
        gross_fen=gross_fen,
        fee_fen=fee_fen,
        net_fen=gross_fen - fee_fen,
        status=FeeStatus.VERIFIED,
        terms_version=schedule.terms_version,
        draws=tuple(draws),
    )


def purchase_fee_fen(purchase_fen: int, schedule: FeeSchedule) -> int:
    """Front-end fee of a purchase: amount times the schedule's purchase rate, half-up to fen."""
    return quantize_fen(Decimal(purchase_fen) * schedule.purchase_rate / 100)


def tier_rate(schedule: FeeSchedule, held_days: int) -> Decimal | None:
    """Rate of the tier that covers `held_days`, or None if no tier does."""
    for tier in schedule.tiers:
        if tier.min_days <= held_days and (tier.max_days is None or held_days <= tier.max_days):
            return tier.rate
    return None


def _draw_fifo(
    lots: Sequence[Lot], shares: Decimal, schedule: FeeSchedule, as_of: date
) -> list[LotDraw] | None:
    remaining = shares
    draws: list[LotDraw] = []
    for lot in sorted(lots, key=lambda lot: (lot.acquired_on, lot.lot_id)):
        if remaining <= 0:
            break
        taken = min(lot.shares, remaining)
        if taken <= 0:
            continue
        held_days = (as_of - lot.acquired_on).days
        if held_days < 0:
            return None
        rate = tier_rate(schedule, held_days)
        if rate is None:
            return None
        draws.append(LotDraw(lot_id=lot.lot_id, shares=taken, held_days=held_days, rate=rate))
        remaining -= taken
    return draws if remaining <= 0 else None
