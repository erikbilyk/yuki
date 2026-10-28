"""Redemption fees: FIFO lots, flat and tiered schedules, unverified quotes (SPEC §14.1, §14.4)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from wg.contracts.engines import FeeSchedule, FeeStatus, FeeTier, Lot, RedemptionFee
from wg.numeric import redemption_fee
from wg.numeric.fees import purchase_fee_fen, tier_rate

TODAY = date(2026, 11, 2)
NAV = Decimal("1.0000")
FLAT = FeeSchedule(
    schedule_id="fee_flat",
    terms_version="flat_v1",
    purchase_rate="0.0000",
    tiers=(FeeTier(min_days=1, max_days=None, rate="0.0030"),),
)
TIERED = FeeSchedule(
    schedule_id="fee_tiered",
    terms_version="tiered_v1",
    purchase_rate="0.0015",
    tiers=(
        FeeTier(min_days=1, max_days=6, rate="0.0150"),
        FeeTier(min_days=7, max_days=29, rate="0.0050"),
        FeeTier(min_days=30, max_days=None, rate="0.0000"),
    ),
)


def lot(name: str, shares: str, age_days: int, product: str = "990003") -> Lot:
    return Lot(
        lot_id=f"lot_{name}",
        product_id=product,
        shares=shares,
        acquired_on=TODAY - timedelta(days=age_days),
        cost_fen=0,
    )


def test_conforms_to_the_engine_protocol() -> None:
    engine: RedemptionFee = redemption_fee
    assert engine is redemption_fee


def test_flat_fee_is_gross_times_rate() -> None:
    quote = redemption_fee((lot("a", "10000", 60),), Decimal("5000"), NAV, FLAT, TODAY)
    assert (quote.gross_fen, quote.fee_fen, quote.net_fen) == (500000, 1500, 498500)
    assert quote.status is FeeStatus.VERIFIED
    assert quote.terms_version == "flat_v1"
    assert [(d.lot_id, str(d.shares), d.held_days) for d in quote.draws] == [
        ("lot_a", "5000.0000", 60)
    ]


@pytest.mark.parametrize(
    ("held_days", "rate"),
    [(1, "0.0150"), (6, "0.0150"), (7, "0.0050"), (29, "0.0050"), (30, "0.0000"), (400, "0.0000")],
)
def test_tier_bounds_are_inclusive_whole_days(held_days: int, rate: str) -> None:
    assert str(tier_rate(TIERED, held_days)) == rate


def test_fifo_draws_oldest_lot_first_at_its_own_rate() -> None:
    lots = (lot("new", "1000", 3), lot("old", "600", 10))
    quote = redemption_fee(lots, Decimal("1000"), NAV, TIERED, TODAY)
    assert [(d.lot_id, str(d.shares), str(d.rate)) for d in quote.draws] == [
        ("lot_old", "600.0000", "0.0050"),
        ("lot_new", "400.0000", "0.0150"),
    ]
    # 600 · 0.5 % + 400 · 1.5 % = 3.00 + 6.00 yuan.
    assert (quote.gross_fen, quote.fee_fen, quote.net_fen) == (100000, 900, 99100)


def test_fifo_ties_on_date_follow_lot_id() -> None:
    lots = (lot("b", "100", 10), lot("a", "100", 10))
    quote = redemption_fee(lots, Decimal("150"), NAV, FLAT, TODAY)
    assert [d.lot_id for d in quote.draws] == ["lot_a", "lot_b"]


def test_fee_is_rounded_once_half_up() -> None:
    # 1.6667 shares at 1.2345: gross 2.05754115 → 2.06; fee 0.3 % = 0.00617 → 0.01.
    quote = redemption_fee((lot("a", "10", 60),), Decimal("1.6667"), Decimal("1.2345"), FLAT, TODAY)
    assert (quote.gross_fen, quote.fee_fen, quote.net_fen) == (206, 1, 205)
    # 5 shares at 1.0000 with 0.3 %: 0.015 yuan rounds up to 0.02.
    quote = redemption_fee((lot("a", "10", 60),), Decimal("5"), NAV, FLAT, TODAY)
    assert quote.fee_fen == 2


@pytest.mark.case("T43")
@pytest.mark.parametrize(
    "lots",
    [(), (lot("a", "4999.9999", 60),)],
    ids=["no lots", "lots short by one ten-thousandth"],
)
def test_lots_that_do_not_cover_the_shares_leave_the_fee_unverified(lots) -> None:
    quote = redemption_fee(lots, Decimal("5000"), NAV, FLAT, TODAY)
    assert quote.status is FeeStatus.UNVERIFIED
    assert quote.fee_fen is None and quote.net_fen is None
    assert quote.gross_fen == 500000
    assert quote.draws == ()


@pytest.mark.parametrize("age_days", [0, -1], ids=["bought today", "acquired after as_of"])
def test_lot_without_a_tier_leaves_the_fee_unverified(age_days: int) -> None:
    quote = redemption_fee((lot("a", "100", age_days),), Decimal("100"), NAV, TIERED, TODAY)
    assert quote.status is FeeStatus.UNVERIFIED


def test_extra_lots_beyond_the_shares_are_not_drawn() -> None:
    lots = (lot("a", "100", 40), lot("b", "100", 20), lot("c", "100", 3))
    quote = redemption_fee(lots, Decimal("100"), NAV, TIERED, TODAY)
    assert [d.lot_id for d in quote.draws] == ["lot_a"]
    assert quote.fee_fen == 0


def test_lots_of_two_funds_are_refused() -> None:
    lots = (lot("a", "100", 40), lot("b", "100", 40, product="990004"))
    with pytest.raises(ValueError):
        redemption_fee(lots, Decimal("10"), NAV, FLAT, TODAY)


@pytest.mark.parametrize("shares", ["0", "-1"])
def test_non_positive_shares_are_refused(shares: str) -> None:
    with pytest.raises(ValueError):
        redemption_fee((lot("a", "100", 40),), Decimal(shares), NAV, FLAT, TODAY)


def test_purchase_fee_uses_the_purchase_rate() -> None:
    assert purchase_fee_fen(498500, FLAT) == 0
    # 4,985.00 · 0.15 % = 7.4775 yuan.
    assert purchase_fee_fen(498500, TIERED) == 748
