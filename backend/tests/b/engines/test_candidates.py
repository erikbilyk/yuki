"""Candidate search: every check of SPEC §14.3, ranking and the single-leg alternatives."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from wg.contracts.engines import (
    CandidateKind,
    CandidateReason,
    CandidateSearchResult,
    ExposureGroup,
    FeeSchedule,
    FeeTier,
    FundProduct,
    HoldingsSnapshot,
    SearchCandidates,
    TargetStatus,
)
from wg.numeric import exposure, search_candidates
from wg.numeric.candidates import inclusive_cap_fen
from wg.textcheck.amounts import find_amounts

from ..fund_builders import LETTERS, changed, fixture_market, position

MARKET = fixture_market()
A, B, C, D = (LETTERS[k] for k in "ABCD")
SECTOR = MARKET.plan["sector"]


def run(**changes) -> CandidateSearchResult:
    return search_candidates(MARKET.search_input(**changes))


def with_product(code: str, **changes) -> tuple[FundProduct, ...]:
    return tuple(changed(p, **changes) if p.product_id == code else p for p in MARKET.products)


def with_snapshot(code: str, **changes) -> tuple[HoldingsSnapshot, ...]:
    return tuple(changed(s, **changes) if s.product_id == code else s for s in MARKET.snapshots)


def swap(result: CandidateSearchResult, sell: str, buy: str, gross_fen: int):
    return next(
        c
        for c in result.candidates
        if c.kind is CandidateKind.SWAP
        and (c.sell_product, c.buy_product, c.gross_fen)
        == (
            sell,
            buy,
            gross_fen,
        )
    )


def extra(result: CandidateSearchResult, kind: CandidateKind):
    return next(c for c in result.candidates if c.kind is kind)


def test_conforms_to_the_engine_protocol() -> None:
    engine: SearchCandidates = search_candidates
    assert engine is search_candidates


def test_reasons_are_empty_exactly_for_passing_candidates() -> None:
    for candidate in run().candidates:
        assert candidate.passes == (candidate.reasons == ())


def test_swaps_are_ranked_by_gross_then_fee() -> None:
    swaps = [c for c in run().candidates if c.kind is CandidateKind.SWAP]
    keys = [(c.gross_fen, c.fee_fen) for c in swaps]
    assert keys == sorted(keys)


def test_fee_breaks_a_tie_between_swaps_of_equal_gross() -> None:
    # With a 45 % target several ¥1,000 swaps pass; A→D comes first on fund code alone.
    loose = run(target_max_pct="45.0000")
    assert loose.selected is not None
    assert (loose.selected.sell_product, loose.selected.buy_product) == (A, D)
    costly = FeeSchedule(
        schedule_id="fee_costly",
        terms_version="costly_v1",
        purchase_rate="0.0000",
        tiers=(FeeTier(min_days=1, max_days=None, rate="0.0150"),),
    )
    result = run(target_max_pct="45.0000", products=with_product(A, fee_schedule=costly))
    assert result.selected is not None
    assert (result.selected.sell_product, result.selected.buy_product) == (C, B)
    assert result.selected.fee_fen == 300
    assert swap(result, A, D, 100000).passes
    assert swap(result, A, D, 100000).fee_fen == 1500


def test_a_different_target_changes_the_choice() -> None:
    # C→D needs about ¥2,921 to reach 42 %, so the grid's ¥3,000 is the smallest passing swap.
    result = run(target_max_pct="42.0000")
    assert result.selected is not None
    selected = result.selected
    assert (selected.sell_product, selected.buy_product, selected.gross_fen) == (C, D, 300000)
    assert not swap(result, C, D, 200000).passes


def test_selected_is_none_when_no_swap_passes() -> None:
    result = run(target_max_pct="30.0000")
    assert result.selected is None
    assert not any(c.passes for c in result.candidates if c.kind is CandidateKind.SWAP)


def test_target_already_met_makes_no_trade_pass() -> None:
    result = run(target_max_pct="46.0000")
    no_trade = extra(result, CandidateKind.NO_TRADE)
    assert no_trade.passes
    for kind in (CandidateKind.SELL_ONLY, CandidateKind.BUY_ONLY):
        alternative = extra(result, kind)
        assert (alternative.gross_fen, alternative.purchase_fen) == (0, 0)
        assert alternative.passes
    # selected is a swap, never no_trade.
    assert result.selected is not None and result.selected.kind is CandidateKind.SWAP


@pytest.mark.case("T44")
def test_strict_fee_limit_fails_at_fifteen_yuan_and_inclusive_passes() -> None:
    (strict,) = find_amounts("手续费低于15元")
    (inclusive,) = find_amounts("手续费不超过15元")
    strict_cap = inclusive_cap_fen(strict.fen, strict=strict.strict)
    inclusive_cap = inclusive_cap_fen(inclusive.fen, strict=inclusive.strict)
    assert (strict_cap, inclusive_cap) == (1499, 1500)
    passing = swap(run(fee_cap_fen=inclusive_cap), C, D, 500000)
    assert passing.fee_fen == 1500 and passing.passes
    failing = swap(run(fee_cap_fen=strict_cap), C, D, 500000)
    assert failing.reasons == (CandidateReason.FEE_CAP,)


def test_turnover_cap_is_inclusive() -> None:
    assert swap(run(turnover_cap_fen=998500), C, D, 500000).passes
    candidate = swap(run(turnover_cap_fen=998499), C, D, 500000)
    assert candidate.reasons == (CandidateReason.TURNOVER_CAP,)


def test_purchase_below_the_fund_minimum_fails() -> None:
    result = run(products=with_product(D, min_purchase_fen=498501))
    assert swap(result, C, D, 500000).reasons == (CandidateReason.PURCHASE_BELOW_MIN,)
    result = run(products=with_product(D, min_purchase_fen=498500))
    assert swap(result, C, D, 500000).passes


def test_purchase_fee_counts_toward_the_fee_cap_and_buys_fewer_shares() -> None:
    schedule = changed(MARKET.product("D").fee_schedule, purchase_rate="0.0015")
    candidate = swap(run(products=with_product(D, fee_schedule=schedule)), C, D, 500000)
    # Redemption ¥15.00 plus 0.15 % of ¥4,985.00 (¥7.48).
    assert candidate.fee_fen == 1500 + 748
    assert candidate.purchase_fen == 498500
    assert candidate.checkpoint.total_fen == 5_000_000 - 1500 - 748
    assert candidate.reasons == (CandidateReason.FEE_CAP,)


def test_buy_only_is_limited_by_headroom() -> None:
    needed = 916667
    assert extra(run(headroom_fen=needed), CandidateKind.BUY_ONLY).passes
    short = extra(run(headroom_fen=needed - 1), CandidateKind.BUY_ONLY)
    assert short.reasons == (CandidateReason.INSUFFICIENT_HEADROOM,)


def test_swap_purchase_is_paid_from_net_proceeds_without_headroom() -> None:
    assert swap(run(headroom_fen=0), C, D, 500000).passes


@pytest.mark.case("T45")
def test_purchase_waits_for_the_redemption_to_settle() -> None:
    # Redemption T+3, purchase settles the same day: T+3 ≤ T+5.
    on_time = run(checkpoint_on=MARKET.epoch + timedelta(days=3))
    assert swap(on_time, C, D, 500000).passes
    early = run(checkpoint_on=MARKET.epoch + timedelta(days=2))
    assert swap(early, C, D, 500000).reasons == (CandidateReason.SETTLEMENT_CALENDAR,)


def test_buy_only_checks_its_own_settlement() -> None:
    products = with_product(D, purchase_settle_days=6)
    candidate = extra(run(products=products, headroom_fen=10_000_000), CandidateKind.BUY_ONLY)
    assert candidate.reasons == (CandidateReason.SETTLEMENT_CALENDAR,)


def test_unresolved_weight_makes_the_target_unverified() -> None:
    snapshots = with_snapshot(D, unresolved_weight="0.5000")
    candidate = swap(run(snapshots=snapshots), C, D, 500000)
    assert candidate.checkpoint.status is TargetStatus.UNVERIFIED
    assert candidate.reasons == (CandidateReason.TARGET_UNVERIFIED,)


def test_non_executable_snapshot_cannot_back_a_target() -> None:
    snapshots = with_snapshot(B, executable=False)
    candidate = swap(run(snapshots=snapshots), C, D, 500000)
    assert candidate.checkpoint.status is TargetStatus.MET
    assert candidate.reasons == (CandidateReason.TARGET_UNVERIFIED,)


def test_redeeming_more_shares_than_held_fails_share_rounding() -> None:
    positions = tuple(
        position(D, Decimal("4000")) if p.product_id == D else p for p in MARKET.positions
    )
    candidate = swap(run(positions=positions), D, A, 500000)
    assert CandidateReason.SHARE_ROUNDING in candidate.reasons


def test_gross_without_an_exact_share_count_fails_share_rounding() -> None:
    # At NAV 300.0000 one share step is ¥0.03, so ¥1,000.00 cannot be redeemed exactly.
    nav = Decimal("300.0000")
    positions = tuple(
        position(C, Decimal("40.0000"), nav) if p.product_id == C else p for p in MARKET.positions
    )
    result = run(positions=positions, products=with_product(C, nav=nav))
    assert CandidateReason.SHARE_ROUNDING in swap(result, C, D, 100000).reasons
    assert CandidateReason.SHARE_ROUNDING not in swap(result, C, D, 300000).reasons


def test_odd_nav_redeems_the_nearest_share_count() -> None:
    nav = Decimal("1.2345")
    positions = tuple(
        position(C, Decimal("8100.4455"), nav) if p.product_id == C else p for p in MARKET.positions
    )
    result = run(positions=positions, products=with_product(C, nav=nav))
    candidate = swap(result, C, D, 100000)
    assert str(candidate.shares_redeemed) == "810.0446"
    assert CandidateReason.SHARE_ROUNDING not in candidate.reasons


def test_sell_only_picks_the_fund_with_the_largest_sector_weight() -> None:
    sell_only = extra(run(), CandidateKind.SELL_ONLY)
    assert sell_only.sell_product == C
    assert sell_only.buy_product is None
    assert sell_only.purchase_fen == 0
    assert sell_only.turnover_fen == sell_only.gross_fen
    assert sell_only.checkpoint.status is TargetStatus.MET
    # One fen less leaves the sector above the target.
    for gross, status in ((sell_only.gross_fen - 1, "not_met"), (sell_only.gross_fen, "met")):
        positions = tuple(
            position(C, Decimal(1_000_000 - gross) / 100) if p.product_id == C else p
            for p in MARKET.positions
        )
        result = exposure(positions, MARKET.snapshots, ExposureGroup.SECTOR, SECTOR, Decimal(40))
        assert result.status == status


def test_buy_only_picks_the_fund_with_the_smallest_sector_weight() -> None:
    buy_only = extra(run(), CandidateKind.BUY_ONLY)
    assert (buy_only.buy_product, buy_only.sell_product) == (D, None)
    assert buy_only.gross_fen == 0
    assert buy_only.turnover_fen == buy_only.purchase_fen


def test_single_leg_that_cannot_reach_the_target_uses_what_it_has() -> None:
    # A 5 % target is below every fund's weight except D's 10 %: no single leg reaches it.
    result = run(target_max_pct="5.0000", headroom_fen=123400, turnover_cap_fen=100_000_000)
    sell_only = extra(result, CandidateKind.SELL_ONLY)
    assert sell_only.gross_fen == 1_000_000
    assert CandidateReason.TARGET_NOT_MET in sell_only.reasons
    buy_only = extra(result, CandidateKind.BUY_ONLY)
    assert buy_only.purchase_fen == 123400
    assert CandidateReason.TARGET_NOT_MET in buy_only.reasons


def test_input_is_not_changed_and_result_has_no_float() -> None:
    inputs = MARKET.search_input()
    before = inputs.model_dump_json()
    result = search_candidates(inputs)
    assert inputs.model_dump_json() == before

    def no_float(node) -> None:
        assert not isinstance(node, float)
        if isinstance(node, dict):
            for value in node.values():
                no_float(value)
        elif isinstance(node, list):
            for value in node:
                no_float(value)

    no_float(json.loads(result.model_dump_json()))
