"""Plan routes (SPEC §19.2): the list, one plan's view and the customer's stop."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from wg.api.guards import route_guard
from wg.contracts.api import PlansView, PlanView
from wg.core.k1_session import Principal
from wg.services.plans import PlanService

router = APIRouter(prefix="/v1")

PlansReader = Annotated[Principal, Depends(route_guard("GET", "/v1/plans"))]
PlanReader = Annotated[Principal, Depends(route_guard("GET", "/v1/plans/{id}"))]
PlanStopper = Annotated[Principal, Depends(route_guard("POST", "/v1/plans/{id}/stop"))]


def _service(request: Request) -> PlanService:
    return request.app.state.plans


@router.get("/plans")
def plans(request: Request, principal: PlansReader) -> PlansView:
    return _service(request).plans(principal.customer_id)


@router.get("/plans/{id}")
def plan(id: str, request: Request, principal: PlanReader) -> PlanView:
    return _service(request).plan(principal.customer_id, id)


@router.post("/plans/{id}/stop")
def stop(id: str, request: Request, principal: PlanStopper) -> PlanView:
    """Stop a plan; a repeat, or a plan already final, returns it unchanged (200)."""
    return _service(request).stop(principal.customer_id, id)
