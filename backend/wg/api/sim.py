"""Simulator routes, mounted only when WG_DEMO_MODE=1 (SPEC §19.2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from wg.api.guards import require_role
from wg.contracts.api import (
    ClockAdvanceRequest,
    ClockView,
    FaultRequest,
    FaultsView,
    RouteRole,
    SimResetRequest,
    SimResetResult,
)
from wg.services.sim import SimService

router = APIRouter(prefix="/sim", dependencies=[Depends(require_role(RouteRole.DEMO))])


def _service(request: Request) -> SimService:
    return request.app.state.sim


@router.post("/reset")
def reset(body: SimResetRequest, request: Request) -> SimResetResult:
    return _service(request).reset(body)


@router.post("/clock/advance")
def advance_clock(body: ClockAdvanceRequest, request: Request) -> ClockView:
    return _service(request).advance_clock(body)


@router.post("/faults")
def arm_fault(body: FaultRequest, request: Request) -> FaultsView:
    return _service(request).arm_fault(body)
