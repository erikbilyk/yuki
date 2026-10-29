"""Fund routes of S3 (SPEC §19.2): positions, exposure, fees, candidates, stress, plan draft.

Reads and calculations are GREEN; the candidate search and the stress run are POST routes
because they store a calc result. Drafting a plan opens its card and approves nothing.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from wg.api.guards import route_guard
from wg.contracts.api import (
    CandidatesRequest,
    CandidatesView,
    ExposureQuery,
    ExposureView,
    FeeEstimateQuery,
    FeeEstimateView,
    PlanDraftResult,
    PositionsView,
    RebalancePlanDraftRequest,
    StressRequest,
    StressView,
)
from wg.core.k1_session import Principal
from wg.services.funds import FundsService

router = APIRouter(prefix="/v1")

ExposureReader = Annotated[Principal, Depends(route_guard("GET", "/v1/funds/exposure"))]
PositionsReader = Annotated[Principal, Depends(route_guard("GET", "/v1/funds/positions"))]
CandidateSearcher = Annotated[Principal, Depends(route_guard("POST", "/v1/funds/candidates"))]
StressRunner = Annotated[Principal, Depends(route_guard("POST", "/v1/funds/stress"))]
FeeEstimator = Annotated[Principal, Depends(route_guard("GET", "/v1/funds/fees/estimate"))]
PlanDrafter = Annotated[Principal, Depends(route_guard("POST", "/v1/funds/plans"))]


def _service(request: Request) -> FundsService:
    return request.app.state.funds


@router.get("/funds/positions")
def positions(request: Request, principal: PositionsReader) -> PositionsView:
    return _service(request).positions(principal.customer_id)


@router.get("/funds/exposure")
def exposure(
    request: Request, principal: ExposureReader, query: Annotated[ExposureQuery, Query()]
) -> ExposureView:
    return _service(request).exposure(principal.customer_id, query)


@router.get("/funds/fees/estimate")
def fee_estimate(
    request: Request, principal: FeeEstimator, query: Annotated[FeeEstimateQuery, Query()]
) -> FeeEstimateView:
    return _service(request).fee_estimate(principal.customer_id, query.product_id, query.shares)


@router.post("/funds/candidates")
def candidates(
    body: CandidatesRequest, request: Request, principal: CandidateSearcher
) -> CandidatesView:
    return _service(request).candidates(principal.customer_id, body)


@router.post("/funds/stress")
def run_stress(body: StressRequest, request: Request, principal: StressRunner) -> StressView:
    return _service(request).stress(principal.customer_id, body.calc_result_id)


@router.post("/funds/plans", status_code=status.HTTP_201_CREATED)
def draft_plan(
    body: RebalancePlanDraftRequest, request: Request, principal: PlanDrafter
) -> PlanDraftResult:
    return _service(request).draft_rebalance_plan(
        principal.customer_id, body.calc_result_id, body.with_bounds
    )
