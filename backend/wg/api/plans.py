"""Plan routes (SPEC §19.2): the list, composition, one plan's view and the customer's stop."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from wg.api.guards import route_guard
from wg.contracts.api import PlanDraftResult, PlansView, PlanView
from wg.contracts.plan import ComposePlanRequest
from wg.core.k1_session import Principal
from wg.services.compose import PlanComposer
from wg.services.plans import PlanService

router = APIRouter(prefix="/v1")

PlansReader = Annotated[Principal, Depends(route_guard("GET", "/v1/plans"))]
PlanReader = Annotated[Principal, Depends(route_guard("GET", "/v1/plans/{id}"))]
PlanComposerRole = Annotated[Principal, Depends(route_guard("POST", "/v1/plans/compose"))]
PlanStopper = Annotated[Principal, Depends(route_guard("POST", "/v1/plans/{id}/stop"))]


def _service(request: Request) -> PlanService:
    return request.app.state.plans


@router.get("/plans")
def plans(request: Request, principal: PlansReader) -> PlansView:
    return _service(request).plans(principal.customer_id)


@router.post("/plans/compose", status_code=status.HTTP_201_CREATED)
def compose(
    body: ComposePlanRequest, request: Request, principal: PlanComposerRole
) -> PlanDraftResult:
    """Join drafts into one plan with its card; a rejected plan is the first validator code."""
    composer: PlanComposer = request.app.state.composer
    return composer.compose(principal.customer_id, body)


@router.get("/plans/{id}")
def plan(id: str, request: Request, principal: PlanReader) -> PlanView:
    return _service(request).plan(principal.customer_id, id)


@router.post("/plans/{id}/stop")
def stop(id: str, request: Request, principal: PlanStopper) -> PlanView:
    """Stop a plan; a repeat, or a plan already final, returns it unchanged (200)."""
    return _service(request).stop(principal.customer_id, id)
