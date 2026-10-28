"""The funds goldens of ACCEPTANCE §3.2, computed from fixture inputs by the engines."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

import pytest

from wg.contracts.engines import (
    CandidateKind,
    CandidateReason,
    ExposureGroup,
    FeeStatus,
    PositionValue,
    TargetStatus,
)
from wg.contracts.money import percent_str
from wg.numeric import exposure, redemption_fee, search_candidates, stress

from ..fund_builders import LETTERS, fixture_market, holdings, shocks
from .data import load_expected

FUNDS = load_expected()["funds"]
MARKET = fixture_market()
SECTOR = MARKET.plan["sector"]
TARGET = Decimal(MARKET.plan["target_max_pct"])
START_FEN = FUNDS["holdings_value_fen"]
FEE_FEN = FUNDS["redemption"]["fee_fen"]


def after_purchase(purchase_fen: int) -> tuple[PositionValue, ...]:
    """Holdings at the checkpoint after redeeming 5,000 of C and buying D for `purchase_fen`."""
    gross = FUNDS["redemption"]["gross_fen"]
    return holdings(
        {
            "A": START_FEN["A"],
            "B": START_FEN["B"],
            "C": START_FEN["C"] - gross,
            "D": START_FEN["D"] + purchase_fen,
        }
    )


def tech(positions: Sequence[PositionValue]):
    return exposure(positions, MARKET.snapshots, ExposureGroup.SECTOR, SECTOR, TARGET)


def d_weight(positions: Sequence[PositionValue]) -> str:
    d = next(p for p in positions if p.product_id == LETTERS["D"])
    return percent_str(Decimal(d.value_fen) * 100 / sum(p.value_fen for p in positions))


def test_holdings_value_at_nav_one() -> None:
    values = {p.product_id: p.value_fen for p in MARKET.positions}
    assert values == {LETTERS[k]: v for k, v in START_FEN.items()}
    assert sum(values.values()) == FUNDS["total_value_fen"]


@pytest.mark.case("T39")
def test_start_technology_share() -> None:
    result = tech(MARKET.positions)
    assert result.total_fen == FUNDS["total_value_fen"]
    assert result.lower_fen == result.upper_fen == FUNDS["technology"]["value_fen"]
    assert str(result.lower_pct) == str(result.upper_pct) == FUNDS["technology"]["pct"]
    assert result.status is TargetStatus.NOT_MET
    assert result.executable
    assert str(result.unresolved_pct) == "0.0000"


def test_issuer_x_exposure() -> None:
    result = exposure(MARKET.positions, MARKET.snapshots, ExposureGroup.ISSUER, "发行人X", None)
    assert result.lower_fen == FUNDS["issuer_x"]["value_fen"]
    assert str(result.lower_pct) == FUNDS["issuer_x"]["pct"]
    assert result.status is None


@pytest.mark.case("T39")
def test_redemption_of_c() -> None:
    golden = FUNDS["redemption"]
    product = MARKET.product(golden["product"])
    quote = redemption_fee(
        MARKET.lots_of(golden["product"]),
        Decimal(golden["shares"]),
        product.nav,
        product.fee_schedule,
        MARKET.epoch,
    )
    assert quote.status is FeeStatus.VERIFIED
    assert (quote.gross_fen, quote.fee_fen, quote.net_fen) == (
        golden["gross_fen"],
        golden["fee_fen"],
        golden["net_fen"],
    )
    assert quote.terms_version == "synthetic_0.3pct_v1"


@pytest.mark.parametrize(
    "name", ["base_plan", "revision"], ids=["T39 base ¥4,985", "T46 revision ¥4,785"]
)
def test_plan_outcome_at_the_checkpoint(name: str) -> None:
    golden = FUNDS[name]
    positions = after_purchase(golden["purchase_fen"])
    result = tech(positions)
    assert result.lower_fen == golden["technology_fen"]
    assert result.total_fen == golden["assets_fen"]
    assert str(result.lower_pct) == golden["technology_pct"]
    assert result.status is TargetStatus.MET
    assert d_weight(positions) == golden["d_weight_pct"]
    assert FUNDS["redemption"]["gross_fen"] + golden["purchase_fen"] == golden["turnover_fen"]


@pytest.mark.case("T47")
def test_hypothetical_purchase_meets_the_target() -> None:
    golden = FUNDS["hypothetical_purchase"]
    result = tech(after_purchase(golden["purchase_fen"]))
    assert str(result.lower_pct) == golden["technology_pct"]
    assert result.status is TargetStatus.MET


@pytest.mark.case("T62")
def test_no_purchase_branch_misses_the_target() -> None:
    golden = FUNDS["no_purchase_branch"]
    positions = holdings({"A": START_FEN["A"], "B": START_FEN["B"], "C": 500000, "D": 500000})
    result = tech(positions)
    assert result.lower_fen == golden["technology_fen"]
    assert result.total_fen == golden["assets_fen"]
    assert str(result.lower_pct) == golden["technology_pct"]
    assert result.status is TargetStatus.NOT_MET


# Candidate search


def swap(result, sell: str, buy: str, gross_fen: int):
    return next(
        c
        for c in result.candidates
        if c.kind is CandidateKind.SWAP
        and (c.sell_product, c.buy_product, c.gross_fen) == (LETTERS[sell], LETTERS[buy], gross_fen)
    )


def extra(result, kind: CandidateKind):
    (candidate,) = (c for c in result.candidates if c.kind is kind)
    return candidate


@pytest.mark.case("T39")
def test_grid_has_sixty_swaps_and_three_extras() -> None:
    grid = FUNDS["candidate_grid"]
    result = search_candidates(MARKET.search_input())
    swaps = [c for c in result.candidates if c.kind is CandidateKind.SWAP]
    assert len(swaps) == grid["swaps"]
    pairs = {(c.sell_product, c.buy_product) for c in swaps}
    assert len(pairs) == grid["ordered_pairs"]
    assert sorted({c.gross_fen for c in swaps}) == grid["amounts_fen"]
    assert [c.kind.value for c in result.candidates[len(swaps) :]] == grid["extra_candidates"]
    assert result.cash_snapshot_id == "snp_epoch"
    assert str(result.start.lower_pct) == FUNDS["technology"]["pct"]


@pytest.mark.case("T39")
def test_exactly_one_grid_candidate_passes_and_is_selected() -> None:
    (golden,) = FUNDS["candidate_grid"]["passing"]
    result = search_candidates(MARKET.search_input())
    passing = [c for c in result.candidates if c.passes]
    assert len(passing) == 1
    (only,) = passing
    assert (only.sell_product, only.buy_product, only.gross_fen) == (
        LETTERS[golden["sell"]],
        LETTERS[golden["buy"]],
        golden["gross_fen"],
    )
    assert result.selected == only


@pytest.mark.case("T39")
def test_selected_swap_matches_the_base_plan() -> None:
    base = FUNDS["base_plan"]
    selected = search_candidates(MARKET.search_input()).selected
    assert selected is not None
    assert selected.fee_fen == FEE_FEN
    assert selected.purchase_fen == base["purchase_fen"]
    assert selected.turnover_fen == base["turnover_fen"]
    assert str(selected.shares_redeemed) == FUNDS["redemption"]["shares"]
    assert selected.checkpoint.lower_fen == base["technology_fen"]
    assert selected.checkpoint.total_fen == base["assets_fen"]
    assert str(selected.checkpoint.lower_pct) == base["technology_pct"]


def test_c_to_d_4000_fails_the_target() -> None:
    golden = FUNDS["swap_c_to_d_4000"]
    candidate = swap(search_candidates(MARKET.search_input()), "C", "D", golden["gross_fen"])
    assert str(candidate.checkpoint.lower_pct) == golden["technology_pct"]
    assert candidate.passes is golden["passes_target"]
    assert candidate.reasons == (CandidateReason.TARGET_NOT_MET,)


def test_continuous_threshold_lies_between_two_fen() -> None:
    golden = FUNDS["continuous_threshold"]
    threshold = golden["gross_fen"]
    inputs = MARKET.search_input(grid_fen=(threshold - 1, threshold))
    result = search_candidates(inputs)
    assert not swap(result, "C", "D", threshold - 1).passes
    assert swap(result, "C", "D", threshold).passes
    grid_choice = FUNDS["candidate_grid"]["passing"][0]["gross_fen"]
    assert grid_choice - threshold == golden["overpay_vs_grid_fen"]


def test_single_leg_alternatives_fail() -> None:
    golden = FUNDS["single_leg"]
    result = search_candidates(MARKET.search_input())
    sell_only = extra(result, CandidateKind.SELL_ONLY)
    buy_only = extra(result, CandidateKind.BUY_ONLY)
    assert sell_only.sell_product == LETTERS["C"]
    assert buy_only.buy_product == LETTERS["D"]
    assert sell_only.gross_fen == golden["needed_gross_fen"]
    assert buy_only.purchase_fen == golden["needed_gross_fen"]
    assert sell_only.fee_fen == golden["sell_only_fee_fen"]
    assert sell_only.reasons == (CandidateReason(golden["sell_only_fails"]),)
    assert buy_only.reasons == (CandidateReason(golden["buy_only_fails"]),)
    # Both reach the target; only the fee cap and the free cash stop them.
    assert sell_only.checkpoint.status is TargetStatus.MET
    assert buy_only.checkpoint.status is TargetStatus.MET


def test_no_trade_fails_the_target() -> None:
    no_trade = extra(search_candidates(MARKET.search_input()), CandidateKind.NO_TRADE)
    assert no_trade.reasons == (CandidateReason.TARGET_NOT_MET,)
    assert (no_trade.gross_fen, no_trade.fee_fen, no_trade.turnover_fen) == (0, 0, 0)
    assert str(no_trade.checkpoint.lower_pct) == FUNDS["technology"]["pct"]


# Variants


@pytest.mark.case("T42")
def test_fee_of_young_lots_exceeds_the_cap() -> None:
    market = fixture_market("t42")
    product = market.product("C")
    quote = redemption_fee(
        market.lots_of("C"), Decimal("5000"), product.nav, product.fee_schedule, market.epoch
    )
    assert quote.fee_fen == load_expected()["variants"]["t42"]["redemption_fee_fen"]
    assert quote.terms_version == "public_c_share_v1"
    assert [(d.held_days, str(d.rate)) for d in quote.draws] == [(20, "0.0050")]
    result = search_candidates(market.search_input())
    candidate = swap(result, "C", "D", 500000)
    assert candidate.fee_fen == 2500
    assert candidate.reasons == (CandidateReason.FEE_CAP,)
    assert result.selected is None


@pytest.mark.case("T43")
def test_unknown_lots_leave_the_fee_unverified() -> None:
    market = fixture_market("t43")
    assert market.lots_of("C") == ()
    product = market.product("C")
    quote = redemption_fee((), Decimal("5000"), product.nav, product.fee_schedule, market.epoch)
    assert quote.status is FeeStatus.UNVERIFIED
    assert quote.fee_fen is None and quote.net_fen is None
    result = search_candidates(market.search_input())
    candidate = swap(result, "C", "D", 500000)
    assert candidate.fee_fen is None
    assert CandidateReason.FEE_UNVERIFIED in candidate.reasons
    assert not any(c.passes for c in result.candidates if c.sell_product == LETTERS["C"])
    assert result.selected is None


@pytest.mark.case("T64")
def test_redemption_settling_at_t4_still_reaches_the_checkpoint() -> None:
    market = fixture_market("t64")
    selected = search_candidates(market.search_input()).selected
    assert selected is not None
    assert (selected.sell_product, selected.buy_product) == (LETTERS["C"], LETTERS["D"])


@pytest.mark.parametrize("variant", ["t52", "t65"])
def test_settlement_after_the_checkpoint_fails_the_calendar(variant: str) -> None:
    market = fixture_market(variant)
    result = search_candidates(market.search_input())
    assert swap(result, "C", "D", 500000).reasons == (CandidateReason.SETTLEMENT_CALENDAR,)
    assert result.selected is None
    # One day more to the checkpoint makes the same swap pass.
    later = market.search_input(checkpoint_on=market.epoch + timedelta(days=6))
    assert swap(search_candidates(later), "C", "D", 500000).passes


# Stress


@pytest.mark.parametrize("plan", ["base", "revision"])
def test_stress_losses_include_the_fee(plan: str) -> None:
    golden = FUNDS["stress"]["loss_fen"]
    purchase = FUNDS["base_plan" if plan == "base" else "revision"]["purchase_fen"]
    result = stress(shocks(), MARKET.positions, after_purchase(purchase), FEE_FEN)
    assert result.fee_fen == FEE_FEN
    assert {r.shock: r.before_loss_fen for r in result.rows} == {
        name: losses["no_trade"] for name, losses in golden.items()
    }
    assert {r.shock: r.after_loss_fen for r in result.rows} == {
        name: losses[plan] for name, losses in golden.items()
    }
    for row in result.rows:
        assert row.change_fen == row.after_loss_fen - row.before_loss_fen


def test_adverse_target_fund_shock_against_no_trade() -> None:
    purchase = FUNDS["base_plan"]["purchase_fen"]
    result = stress(shocks(), MARKET.positions, after_purchase(purchase), FEE_FEN)
    rows = {r.shock: r for r in result.rows}
    assert rows["target_fund"].change_fen == FUNDS["stress"]["adverse_target_fund_vs_no_trade_fen"]
    assert all(r.change_fen < 0 for name, r in rows.items() if name != "target_fund")
