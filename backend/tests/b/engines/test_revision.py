"""The purchase revision rule on plain numbers, with seeded property checks (SPEC §14.6)."""

from __future__ import annotations

import random

import pytest

from wg.contracts.engines import DecidePurchase, PlanBounds, RevisionOutcome
from wg.contracts.errors import ErrorCode
from wg.numeric import decide_purchase

BOUNDS = PlanBounds(
    p0_fen=498500,
    spare0_fen=100000,
    max_added_fen=120000,
    max_retained_fen=20000,
    min_purchase_fen=478500,
)
P0 = BOUNDS.p0_fen
BOUND_REASONS = {
    ErrorCode.BILL_INCREASE_OVER_LIMIT,
    ErrorCode.RETENTION_OVER_LIMIT,
    ErrorCode.PURCHASE_BELOW_MIN,
}


def test_conforms_to_the_engine_protocol() -> None:
    engine: DecidePurchase = decide_purchase
    assert engine is decide_purchase


@pytest.mark.parametrize("x_fen", [0, 1, 99999, 100000])
def test_bills_within_the_spare_cash_leave_the_purchase(x_fen: int) -> None:
    decision = decide_purchase(x_fen, BOUNDS, P0, P0)
    assert (decision.outcome, decision.purchase_fen) == (RevisionOutcome.UNCHANGED, P0)


def test_first_fen_beyond_the_spare_cash_revises() -> None:
    decision = decide_purchase(100001, BOUNDS, P0, P0)
    assert (decision.outcome, decision.purchase_fen) == (RevisionOutcome.REVISE, P0 - 1)


@pytest.mark.parametrize(
    ("x_fen", "reasons"),
    [
        (120000, ()),
        (
            120001,
            (
                ErrorCode.BILL_INCREASE_OVER_LIMIT,
                ErrorCode.RETENTION_OVER_LIMIT,
                ErrorCode.PURCHASE_BELOW_MIN,
            ),
        ),
    ],
)
def test_bounds_are_inclusive(x_fen: int, reasons: tuple[ErrorCode, ...]) -> None:
    decision = decide_purchase(x_fen, BOUNDS, P0, P0)
    assert decision.reasons == reasons


def test_each_bound_is_checked_on_its_own() -> None:
    wide = BOUNDS.model_copy(update={"max_added_fen": 10**9, "max_retained_fen": 10**9})
    decision = decide_purchase(130000, wide, P0, P0)
    assert decision.reasons == (ErrorCode.PURCHASE_BELOW_MIN,)
    loose_min = wide.model_copy(update={"min_purchase_fen": 0})
    decision = decide_purchase(130000, loose_min, P0, P0)
    assert (decision.outcome, decision.purchase_fen) == (RevisionOutcome.REVISE, 468500)
    retained_only = BOUNDS.model_copy(update={"max_added_fen": 10**9, "min_purchase_fen": 0})
    decision = decide_purchase(130000, retained_only, P0, P0)
    assert decision.reasons == (ErrorCode.RETENTION_OVER_LIMIT,)


def test_bounds_are_measured_from_the_baseline_not_the_last_revision() -> None:
    # After a revision to ¥4,785 the current order changes, the bounds do not.
    from_p0 = decide_purchase(160000, BOUNDS, P0, 10**9)
    from_revision = decide_purchase(160000, BOUNDS, 478500, 10**9)
    assert from_p0.reasons == from_revision.reasons
    assert from_p0.purchase_fen == from_revision.purchase_fen == 438500


def test_purchase_above_headroom_pauses_with_insufficient_headroom() -> None:
    decision = decide_purchase(80000, BOUNDS, P0, P0 - 1)
    assert decision.outcome is RevisionOutcome.PAUSE
    assert decision.purchase_fen == P0
    assert decision.reasons == (ErrorCode.INSUFFICIENT_HEADROOM,)


def test_headroom_reason_comes_after_the_bound_reasons() -> None:
    decision = decide_purchase(160000, BOUNDS, P0, 0)
    assert decision.reasons[-1] is ErrorCode.INSUFFICIENT_HEADROOM
    assert set(decision.reasons[:-1]) == BOUND_REASONS


def test_without_bounds_the_exact_order_never_changes() -> None:
    for x_fen in (0, 50000, 10**7):
        decision = decide_purchase(x_fen, None, P0, P0)
        assert (decision.outcome, decision.purchase_fen) == (RevisionOutcome.UNCHANGED, P0)


def test_negative_increase_is_refused() -> None:
    with pytest.raises(ValueError):
        decide_purchase(-1, BOUNDS, P0, P0)


def random_bounds(rng: random.Random) -> PlanBounds:
    p0 = rng.randint(0, 1_000_000)
    return PlanBounds(
        p0_fen=p0,
        spare0_fen=rng.randint(0, 300_000),
        max_added_fen=rng.randint(0, 400_000),
        max_retained_fen=rng.randint(0, 300_000),
        min_purchase_fen=rng.randint(0, p0),
    )


def test_property_never_exceeds_headroom_and_reasons_only_when_the_amount_moves() -> None:
    rng = random.Random(20261102)
    for _ in range(3000):
        bounds = random_bounds(rng) if rng.random() < 0.8 else None
        x_fen = rng.randint(0, 600_000)
        current = (
            rng.randint(0, 1_000_000)
            if bounds is None
            else rng.choice([bounds.p0_fen, rng.randint(0, bounds.p0_fen)])
        )
        headroom = rng.randint(0, 1_200_000)
        decision = decide_purchase(x_fen, bounds, current, headroom)

        assert (decision.outcome is RevisionOutcome.PAUSE) == bool(decision.reasons)
        if decision.outcome is not RevisionOutcome.PAUSE:
            assert decision.purchase_fen <= headroom
        if decision.outcome is RevisionOutcome.UNCHANGED:
            assert decision.purchase_fen == current
        if decision.outcome is RevisionOutcome.REVISE:
            assert decision.purchase_fen != current
        if bounds is None:
            assert decision.purchase_fen == current
            assert set(decision.reasons) <= {ErrorCode.EXACT_ORDERS_ONLY}
            continue
        expected = bounds.p0_fen - max(0, x_fen - bounds.spare0_fen)
        assert decision.purchase_fen == expected
        if expected == bounds.p0_fen:
            assert not BOUND_REASONS & set(decision.reasons)
        assert ErrorCode.EXACT_ORDERS_ONLY not in decision.reasons


def test_property_same_inputs_same_decision() -> None:
    rng = random.Random(7)
    for _ in range(500):
        bounds = random_bounds(rng)
        args = (rng.randint(0, 600_000), bounds, bounds.p0_fen, rng.randint(0, 1_200_000))
        assert decide_purchase(*args) == decide_purchase(*args)
