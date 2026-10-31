"""Routes of direct-debit agreements and merchants' requests (S5-SPEC §3.5).

A decision draft from the guided UI names only the request and the decision; the amount and
the account come from the stored request and agreement. A change draft names the agreement
and the change; its kind and version come from the stored agreement.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from wg.api.guards import route_guard
from wg.contracts.api import (
    DebitDecisionRequest,
    DebitRequestsQuery,
    DebitRequestsView,
    DebitRequestView,
    DraftResult,
    MandateChangeRequest,
    MandatesView,
    MerchantsView,
    SubscriptionSpendQuery,
    SubscriptionSpendView,
)
from wg.core.k1_session import Principal
from wg.services.mandates import MandateService

router = APIRouter(prefix="/v1")

MandatesReader = Annotated[Principal, Depends(route_guard("GET", "/v1/mandates"))]
MerchantsReader = Annotated[Principal, Depends(route_guard("GET", "/v1/merchants"))]
RequestsReader = Annotated[Principal, Depends(route_guard("GET", "/v1/debit-requests"))]
RequestReader = Annotated[Principal, Depends(route_guard("GET", "/v1/debit-requests/{id}"))]
SpendReader = Annotated[Principal, Depends(route_guard("GET", "/v1/subscriptions/spend"))]
DecisionDrafter = Annotated[Principal, Depends(route_guard("POST", "/v1/drafts/debit-decision"))]
ChangeDrafter = Annotated[Principal, Depends(route_guard("POST", "/v1/drafts/mandate-change"))]


def _service(request: Request) -> MandateService:
    return request.app.state.mandates


@router.get("/mandates")
def list_mandates(request: Request, principal: MandatesReader) -> MandatesView:
    return _service(request).mandates(principal.customer_id)


@router.get("/merchants")
def merchants(request: Request, principal: MerchantsReader) -> MerchantsView:
    return _service(request).merchants()


@router.get("/debit-requests")
def debit_requests(
    request: Request, principal: RequestsReader, query: Annotated[DebitRequestsQuery, Query()]
) -> DebitRequestsView:
    return _service(request).debit_requests(principal.customer_id, query)


@router.get("/debit-requests/{id}")
def debit_request(id: str, request: Request, principal: RequestReader) -> DebitRequestView:
    return _service(request).debit_request(principal.customer_id, id)


@router.get("/subscriptions/spend")
def subscription_spend(
    request: Request, principal: SpendReader, query: Annotated[SubscriptionSpendQuery, Query()]
) -> SubscriptionSpendView:
    return _service(request).subscription_spend(principal.customer_id, query)


@router.post("/drafts/debit-decision", status_code=status.HTTP_201_CREATED)
def draft_debit_decision(
    body: DebitDecisionRequest, request: Request, principal: DecisionDrafter
) -> DraftResult:
    return _service(request).draft_debit_decision(principal.customer_id, body)


@router.post("/drafts/mandate-change", status_code=status.HTTP_201_CREATED)
def draft_mandate_change(
    body: MandateChangeRequest, request: Request, principal: ChangeDrafter
) -> DraftResult:
    return _service(request).draft_mandate_change(principal.customer_id, body)
