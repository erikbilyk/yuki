from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from wg.contracts.engines import (
    CandidateSearchInput,
    DecidePurchase,
    FeeQuote,
    FeeStatus,
    PlanBounds,
    PositionValue,
    RevisionDecision,
    RevisionOutcome,
)
from wg.contracts.errors import ErrorCode


def test_position_value_refuses_float_shares():
    with pytest.raises(ValidationError):
        PositionValue(product_id="990003", shares=5000.0, nav="1.0000", value_fen=500000)
    position = PositionValue(product_id="990003", shares="5000", nav="1", value_fen=500000)
    assert position.model_dump(mode="json")["shares"] == "5000.0000"


def test_revision_decision_has_reasons_only_when_paused():
    RevisionDecision(outcome=RevisionOutcome.REVISE, x_fen=120000, purchase_fen=478500, reasons=())
    RevisionDecision(
        outcome=RevisionOutcome.PAUSE,
        x_fen=160000,
        purchase_fen=438500,
        reasons=(ErrorCode.BILL_INCREASE_OVER_LIMIT,),
    )
    with pytest.raises(ValidationError):
        RevisionDecision(outcome=RevisionOutcome.PAUSE, x_fen=1, purchase_fen=1, reasons=())
    with pytest.raises(ValidationError):
        RevisionDecision(
            outcome=RevisionOutcome.UNCHANGED,
            x_fen=0,
            purchase_fen=498500,
            reasons=(ErrorCode.PURCHASE_BELOW_MIN,),
        )


def test_unverified_fee_quote_has_no_fee():
    common = {"shares": "5000", "gross_fen": 500000, "terms_version": "v", "draws": ()}
    FeeQuote(**common, fee_fen=None, net_fen=None, status=FeeStatus.UNVERIFIED)
    with pytest.raises(ValidationError):
        FeeQuote(**common, fee_fen=0, net_fen=500000, status=FeeStatus.UNVERIFIED)
    with pytest.raises(ValidationError):
        FeeQuote(**common, fee_fen=None, net_fen=None, status=FeeStatus.VERIFIED)


def test_candidate_search_defaults_to_the_spec_grid():
    search = CandidateSearchInput(
        as_of=date(2026, 11, 2),
        checkpoint_on=date(2026, 11, 7),
        positions=(),
        snapshots=(),
        products=(),
        lots=(),
        sector="technology",
        target_max_pct=Decimal("40"),
        fee_cap_fen=2000,
        turnover_cap_fen=1000000,
        headroom_fen=100000,
        cash_snapshot_id="snp_1",
    )
    assert search.grid_fen == (100000, 200000, 300000, 400000, 500000)


def test_models_are_frozen():
    bounds = PlanBounds(
        p0_fen=498500,
        spare0_fen=100000,
        max_added_fen=120000,
        max_retained_fen=20000,
        min_purchase_fen=478500,
    )
    with pytest.raises(ValidationError):
        bounds.p0_fen = 1


def test_a_function_with_the_contract_signature_fits_the_protocol():
    def decide_purchase(
        x_fen: int, bounds: PlanBounds | None, current_fen: int, headroom_fen: int
    ) -> RevisionDecision:
        return RevisionDecision(
            outcome=RevisionOutcome.UNCHANGED, x_fen=x_fen, purchase_fen=current_fen, reasons=()
        )

    engine: DecidePurchase = decide_purchase
    assert engine(x_fen=0, bounds=None, current_fen=498500, headroom_fen=598500).purchase_fen == (
        498500
    )
