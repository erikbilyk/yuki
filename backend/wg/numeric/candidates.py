"""Grid search for the smallest passing swap that brings a sector under its target (SPEC §14.3).

The result is the smallest passing swap in the grid, not an optimal trade: the grid is fixed
so that the outcome is predictable and can be checked by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from itertools import permutations

from wg.contracts.engines import (
    Candidate,
    CandidateKind,
    CandidateReason,
    CandidateSearchInput,
    CandidateSearchResult,
    ExposureGroup,
    ExposureResult,
    FundProduct,
    HoldingsSnapshot,
    Lot,
    PositionValue,
    TargetStatus,
)
from wg.contracts.money import quantize_fen
from wg.numeric.exposure import disclosed_weight, exposure
from wg.numeric.fees import purchase_fee_fen, redemption_fee

FOUR_PLACES = Decimal("0.0001")
REASON_ORDER = tuple(CandidateReason)


def inclusive_cap_fen(limit_fen: int, *, strict: bool) -> int:
    """The largest fen amount allowed by a limit; "低于15元" (strict) allows at most 1499 fen."""
    return limit_fen - 1 if strict else limit_fen


def search_candidates(inputs: CandidateSearchInput) -> CandidateSearchResult:
    """Evaluate every grid swap plus no_trade, sell_only and buy_only, and pick the best swap."""
    market = _Market.of(inputs)
    start = market.sector_exposure(inputs.positions)
    swaps = [
        market.evaluate(CandidateKind.SWAP, sell=sell, gross_fen=gross, buy=buy)
        for sell, buy in permutations(sorted(market.products), 2)
        for gross in inputs.grid_fen
    ]
    swaps.sort(key=_rank)
    extras = (
        market.evaluate(CandidateKind.NO_TRADE),
        market.sell_only(),
        market.buy_only(),
    )
    return CandidateSearchResult(
        start=start,
        candidates=(*swaps, *extras),
        selected=next((c for c in swaps if c.passes), None),
        cash_snapshot_id=inputs.cash_snapshot_id,
    )


def _rank(candidate: Candidate) -> tuple[int, bool, int, str, str]:
    fee = candidate.fee_fen
    return (
        candidate.gross_fen,
        fee is None,
        fee or 0,
        candidate.sell_product or "",
        candidate.buy_product or "",
    )


@dataclass(frozen=True)
class _Market:
    inputs: CandidateSearchInput
    products: dict[str, FundProduct]
    positions: dict[str, PositionValue]
    snapshots: dict[str, HoldingsSnapshot]
    lots: dict[str, tuple[Lot, ...]]

    @classmethod
    def of(cls, inputs: CandidateSearchInput) -> _Market:
        return cls(
            inputs=inputs,
            products={p.product_id: p for p in inputs.products},
            positions={p.product_id: p for p in inputs.positions},
            snapshots={s.product_id: s for s in inputs.snapshots},
            lots={
                code: tuple(lot for lot in inputs.lots if lot.product_id == code)
                for code in {lot.product_id for lot in inputs.lots}
            },
        )

    def sector_exposure(self, positions: Sequence[PositionValue]) -> ExposureResult:
        return exposure(
            positions,
            self.inputs.snapshots,
            ExposureGroup.SECTOR,
            self.inputs.sector,
            self.inputs.target_max_pct,
        )

    def upper_weight(self, product_id: str) -> Decimal:
        """Disclosed plus unresolved sector weight of one fund; 1 without a snapshot."""
        snapshot = self.snapshots.get(product_id)
        if snapshot is None:
            return Decimal(1)
        weight = disclosed_weight(snapshot, ExposureGroup.SECTOR, self.inputs.sector)
        return weight + snapshot.unresolved_weight

    def excess_fen(self) -> Decimal:
        """Exact upper sector value above the target share of the total, in fen."""
        positions = [p for p in self.positions.values() if p.value_fen > 0]
        upper = sum((p.value_fen * self.upper_weight(p.product_id) for p in positions), Decimal(0))
        total = sum(p.value_fen for p in positions)
        return upper - self.inputs.target_max_pct / 100 * total

    def sell_only(self) -> Candidate:
        """Redeem from the fund with the largest sector weight just enough to meet the target."""
        held = [c for c, p in self.positions.items() if p.value_fen > 0 and c in self.products]
        sell = max(sorted(held), key=self.upper_weight)
        weight = self.upper_weight(sell)
        target = self.inputs.target_max_pct / 100
        excess = self.excess_fen()
        value = self.positions[sell].value_fen
        if excess <= 0:
            gross = 0
        elif weight > target:
            # Selling a fund of weight w takes w per yuan from the sector and 1 from the total.
            gross = min(value, _ceil_fen(excess / (weight - target)))
        else:
            gross = value
        nav = self.products[sell].nav
        shares = (Decimal(gross) / 100 / nav).quantize(FOUR_PLACES, rounding=ROUND_CEILING)
        shares = min(shares, self.positions[sell].shares)
        return self.evaluate(
            CandidateKind.SELL_ONLY, sell=sell, gross_fen=quantize_fen(shares * nav), shares=shares
        )

    def buy_only(self) -> Candidate:
        """Buy the fund with the smallest sector weight, from free cash, to meet the target."""
        buy = min(sorted(self.products), key=self.upper_weight)
        weight = self.upper_weight(buy)
        target = self.inputs.target_max_pct / 100
        excess = self.excess_fen()
        if excess <= 0:
            purchase = 0
        elif weight < target:
            invested = excess / (target - weight)
            rate = self.products[buy].fee_schedule.purchase_rate
            purchase = _ceil_fen(invested / (1 - rate))
        else:
            purchase = self.inputs.headroom_fen
        return self.evaluate(CandidateKind.BUY_ONLY, buy=buy, purchase_fen=purchase)

    def evaluate(
        self,
        kind: CandidateKind,
        *,
        sell: str | None = None,
        gross_fen: int = 0,
        shares: Decimal | None = None,
        buy: str | None = None,
        purchase_fen: int | None = None,
    ) -> Candidate:
        """Build one candidate and run every check on it.

        A swap reinvests the whole net proceeds (`purchase_fen` None). Without verified lots the
        net is unknown and the gross stands in for it; such a candidate fails `FEE_UNVERIFIED`.
        """
        inputs = self.inputs
        after = dict(self.positions)
        leg = _Redemption.none(inputs.as_of)
        if sell is not None and gross_fen > 0:
            leg = self.redeem(sell, gross_fen, shares)
            after[sell] = _reduced(self.positions.get(sell), self.products[sell], leg)
        reasons = set(leg.reasons)
        fee_fen = leg.fee_fen
        settled_on = leg.settled_on

        purchase = 0
        if buy is not None:
            product = self.products[buy]
            purchase = leg.net_fen if purchase_fen is None else purchase_fen
            if purchase > 0:
                purchase_fee = purchase_fee_fen(purchase, product.fee_schedule)
                if fee_fen is not None:
                    fee_fen += purchase_fee
                if purchase < product.min_purchase_fen:
                    reasons.add(CandidateReason.PURCHASE_BELOW_MIN)
                # Purchases are paid from settled cash only (T45): headroom plus net proceeds.
                if purchase > inputs.headroom_fen + leg.net_fen:
                    reasons.add(CandidateReason.INSUFFICIENT_HEADROOM)
                after[buy] = _increased(self.positions.get(buy), product, purchase - purchase_fee)
                settled_on += timedelta(days=product.purchase_settle_days)

        if fee_fen is not None and fee_fen > inputs.fee_cap_fen:
            reasons.add(CandidateReason.FEE_CAP)
        turnover = gross_fen + purchase
        if turnover > inputs.turnover_cap_fen:
            reasons.add(CandidateReason.TURNOVER_CAP)
        if settled_on > inputs.checkpoint_on:
            reasons.add(CandidateReason.SETTLEMENT_CALENDAR)
        checkpoint = self.sector_exposure([p for p in after.values() if p.shares > 0])
        reasons.update(_target_reasons(checkpoint))

        ordered = tuple(r for r in REASON_ORDER if r in reasons)
        return Candidate(
            kind=kind,
            sell_product=sell,
            buy_product=buy,
            gross_fen=gross_fen,
            fee_fen=fee_fen,
            purchase_fen=purchase,
            shares_redeemed=leg.shares,
            turnover_fen=turnover,
            checkpoint=checkpoint,
            passes=not ordered,
            reasons=ordered,
        )

    def redeem(self, sell: str, gross_fen: int, shares: Decimal | None) -> _Redemption:
        """Redeem `gross_fen` of one fund; `shares` defaults to the gross at the fund's NAV."""
        product = self.products[sell]
        held = self.positions.get(sell)
        held_shares = held.shares if held is not None else Decimal(0)
        if shares is None:
            shares = (Decimal(gross_fen) / 100 / product.nav).quantize(
                FOUR_PLACES, rounding=ROUND_HALF_UP
            )
        reasons: set[CandidateReason] = set()
        # No four-place share count redeems exactly this gross, or more shares than are held.
        if shares <= 0 or shares > held_shares or quantize_fen(shares * product.nav) != gross_fen:
            reasons.add(CandidateReason.SHARE_ROUNDING)
        fee_fen: int | None = None
        net_fen = gross_fen
        if shares > 0:
            schedule, as_of = product.fee_schedule, self.inputs.as_of
            quote = redemption_fee(self.lots.get(sell, ()), shares, product.nav, schedule, as_of)
            if quote.fee_fen is not None and quote.net_fen is not None:
                fee_fen, net_fen = quote.fee_fen, quote.net_fen
        if fee_fen is None:
            reasons.add(CandidateReason.FEE_UNVERIFIED)
        redeemed = max(Decimal(0), min(shares, held_shares))
        return _Redemption(
            shares=redeemed,
            fee_fen=fee_fen,
            net_fen=net_fen,
            gross_fen=gross_fen,
            settled_on=self.inputs.as_of + timedelta(days=product.redeem_settle_days),
            reasons=frozenset(reasons),
        )


@dataclass(frozen=True)
class _Redemption:
    shares: Decimal
    gross_fen: int
    fee_fen: int | None
    net_fen: int
    settled_on: date
    reasons: frozenset[CandidateReason]

    @classmethod
    def none(cls, on: date) -> _Redemption:
        return cls(Decimal(0), 0, 0, 0, on, frozenset())


def _target_reasons(checkpoint: ExposureResult) -> set[CandidateReason]:
    if checkpoint.status is TargetStatus.NOT_MET:
        return {CandidateReason.TARGET_NOT_MET}
    if checkpoint.status is TargetStatus.UNVERIFIED or not checkpoint.executable:
        return {CandidateReason.TARGET_UNVERIFIED}
    return set()


def _ceil_fen(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _reduced(held: PositionValue | None, product: FundProduct, leg: _Redemption) -> PositionValue:
    if held is None:
        return PositionValue(
            product_id=product.product_id, shares=Decimal(0), nav=product.nav, value_fen=0
        )
    return held.model_copy(
        update={
            "shares": held.shares - leg.shares,
            "value_fen": max(0, held.value_fen - leg.gross_fen),
        }
    )


def _increased(
    held: PositionValue | None, product: FundProduct, invested_fen: int
) -> PositionValue:
    bought = (Decimal(invested_fen) / 100 / product.nav).quantize(FOUR_PLACES, rounding=ROUND_DOWN)
    added_fen = quantize_fen(bought * product.nav)
    if held is None:
        return PositionValue(
            product_id=product.product_id, shares=bought, nav=product.nav, value_fen=added_fen
        )
    return held.model_copy(
        update={"shares": held.shares + bought, "value_fen": held.value_fen + added_fen}
    )
