"""Trusted routes: cards, confirmation, step-up codes and the simulated SMS panel (SPEC §9).

These routes are for the customer's own session only; no agent tool reaches them.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from wg.api.guards import route_guard
from wg.contracts.api import (
    Card,
    ChallengeView,
    ConfirmRequest,
    ConfirmResult,
    SmsView,
    VerifyRequest,
    VerifyResult,
)
from wg.core.k1_session import Principal
from wg.services.trusted import TrustedService

router = APIRouter(prefix="/v1")

CardReader = Annotated[Principal, Depends(route_guard("GET", "/v1/cards/{id}"))]
CardConfirmer = Annotated[Principal, Depends(route_guard("POST", "/v1/cards/{id}/confirm"))]
CodeRequester = Annotated[Principal, Depends(route_guard("POST", "/v1/actions/{id}/challenge"))]
CodeVerifier = Annotated[Principal, Depends(route_guard("POST", "/v1/challenges/{id}/verify"))]
CodeReissuer = Annotated[Principal, Depends(route_guard("POST", "/v1/challenges/{id}/reissue"))]
SmsReader = Annotated[Principal, Depends(route_guard("GET", "/v1/sms"))]


def _service(request: Request) -> TrustedService:
    return request.app.state.trusted


@router.get("/cards/{id}")
def get_card(id: str, request: Request, principal: CardReader) -> Card:
    return _service(request).card(principal.customer_id, id)


@router.post("/cards/{id}/confirm")
def confirm_card(
    id: str, body: ConfirmRequest, request: Request, response: Response, principal: CardConfirmer
) -> ConfirmResult:
    """The client sends the terms hash of the card it displayed (201; 200 for a repeat)."""
    result, created = _service(request).confirm(principal.customer_id, id, body.terms_hash)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return result


@router.post("/actions/{id}/challenge", status_code=status.HTTP_201_CREATED)
def request_challenge(id: str, request: Request, principal: CodeRequester) -> ChallengeView:
    return _service(request).request_challenge(principal.customer_id, id)


@router.post("/challenges/{id}/verify")
def verify(id: str, body: VerifyRequest, request: Request, principal: CodeVerifier) -> VerifyResult:
    return _service(request).verify(principal.customer_id, id, body.code)


@router.post("/challenges/{id}/reissue", status_code=status.HTTP_201_CREATED)
def reissue(id: str, request: Request, principal: CodeReissuer) -> ChallengeView:
    return _service(request).reissue(principal.customer_id, id)


@router.get("/sms")
def sms(request: Request, principal: SmsReader) -> SmsView:
    return _service(request).sms(principal.customer_id)
