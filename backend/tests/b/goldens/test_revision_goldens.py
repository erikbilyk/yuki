"""The purchase revision sequence of SPEC §14.6 and R1, with the bounds from the fixture."""

from __future__ import annotations

from typing import Any

import pytest

from wg.contracts.engines import PlanBounds, RevisionOutcome
from wg.contracts.errors import ErrorCode
from wg.numeric import decide_purchase

from .data import load_expected, load_fixture

SEQUENCE = load_expected()["revision_sequence"]
S6 = load_expected()["s6"]
BOUNDS = PlanBounds(**load_fixture()["plan_constants"]["bounds"])
P0 = BOUNDS.p0_fen
# Headroom at the T+3 settlement before any new bill.
SETTLED_HEADROOM = S6["base"]["t3_redemption_settled"]["headroom_fen"]
EVENT_FEN = {"bill_800": 80000, "bill_400": 40000, "bill_400_again": 40000}


def test_fixture_bounds_match_the_goldens() -> None:
    assert BOUNDS.model_dump() == SEQUENCE["bounds"]


def x_from_events(delivered: list[str]) -> int:
    """Cumulative bill increase counted once per commitment event, as A computes it."""
    return sum(EVENT_FEN[event] for event in dict.fromkeys(delivered))


def check(step: dict[str, Any]) -> None:
    decision = decide_purchase(step["x_fen"], BOUNDS, step["current_fen"], step["headroom_fen"])
    assert decision.outcome is RevisionOutcome(step["decision"])
    assert decision.purchase_fen == step["purchase_fen"]
    assert decision.x_fen == step["x_fen"]
    assert list(decision.reasons) == step.get("reasons", [])


@pytest.mark.case("R1")
def test_sequence_800_400_400() -> None:
    delivered: list[str] = []
    for step in SEQUENCE["steps"]:
        delivered.append(step["event"])
        assert x_from_events(delivered) == step["x_fen"]
        # Headroom after the settlement falls by every confirmed bill increase.
        assert step["headroom_fen"] == SETTLED_HEADROOM - step["x_fen"]
        check(step)


@pytest.mark.case("R1")
def test_repeated_events_change_nothing() -> None:
    # Each event is delivered again right after its first delivery has been applied.
    events = [step["event"] for step in SEQUENCE["steps"]]
    for repeat in SEQUENCE["repeats"]:
        delivered = events[: events.index(repeat["event"]) + 1] + [repeat["event"]]
        assert x_from_events(delivered) == repeat["x_fen"]
        check(repeat)


@pytest.mark.case("R1")
def test_repeats_after_the_pause_keep_the_pause() -> None:
    last = SEQUENCE["steps"][-1]
    delivered = [step["event"] for step in SEQUENCE["steps"]] + ["bill_400", "bill_400_again"]
    assert x_from_events(delivered) == last["x_fen"]
    check(last)


@pytest.mark.case("R1")
def test_rejected_sketch_value_never_appears() -> None:
    # The faulty sketch measured the third bill against the last revision (¥4,785 − ¥200).
    sketch = SEQUENCE["rejected_sketch_purchase_fen"]
    for step in SEQUENCE["steps"] + SEQUENCE["repeats"]:
        decision = decide_purchase(step["x_fen"], BOUNDS, step["current_fen"], step["headroom_fen"])
        assert decision.purchase_fen != sketch


@pytest.mark.case("T47")
def test_pause_at_1600_reports_the_feasible_amount_and_all_three_reasons() -> None:
    golden_purchase = load_expected()["funds"]["hypothetical_purchase"]["purchase_fen"]
    decision = decide_purchase(
        160000, BOUNDS, 478500, S6["branch_1600"]["t3_bill_confirmed"]["headroom_fen"]
    )
    assert decision.outcome is RevisionOutcome.PAUSE
    assert decision.purchase_fen == golden_purchase
    assert decision.reasons == (
        ErrorCode.BILL_INCREASE_OVER_LIMIT,
        ErrorCode.RETENTION_OVER_LIMIT,
        ErrorCode.PURCHASE_BELOW_MIN,
    )


@pytest.mark.case("T46")
def test_1200_branch_revises_to_the_headroom() -> None:
    headroom = S6["branch_1200"]["t3_bill_confirmed"]["headroom_fen"]
    decision = decide_purchase(120000, BOUNDS, P0, headroom)
    assert decision.outcome is RevisionOutcome.REVISE
    assert decision.purchase_fen == S6["branch_1200"]["t3_purchase"]["purchase_fen"] == headroom


@pytest.mark.case("T46")
def test_500_bill_without_bounds_keeps_the_exact_order() -> None:
    golden = S6["bill_500_without_bounds"]
    decision = decide_purchase(golden["bill_fen"], None, P0, golden["headroom_fen"])
    assert decision.outcome is RevisionOutcome.UNCHANGED
    assert decision.purchase_fen == P0
    assert decision.reasons == ()
    assert golden["purchase_paused"] is False


@pytest.mark.case("T46")
def test_1200_bill_without_bounds_pauses_the_exact_order() -> None:
    headroom = S6["branch_1200"]["t3_bill_confirmed"]["headroom_fen"]
    decision = decide_purchase(120000, None, P0, headroom)
    assert decision.outcome is RevisionOutcome.PAUSE
    assert decision.purchase_fen == P0
    assert decision.reasons == (ErrorCode.EXACT_ORDERS_ONLY,)
