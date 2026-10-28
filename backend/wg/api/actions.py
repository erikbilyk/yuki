"""Draft routes of the guided UI and the customer's approval request and cancel (SPEC §19.2).

A draft made in the guided UI comes from the customer's own form, so its payee and amount
are labelled `user_utterance`; the remark is stored as untrusted text.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from wg.api.guards import route_guard
from wg.contracts.api import (
    ApprovalRequestResult,
    CancelResult,
    DraftResult,
    TransferDraftRequest,
)
from wg.contracts.enums import Provenance
from wg.core.k1_session import Principal
from wg.services.actions import ActionService, Slot, TransferDraft

router = APIRouter(prefix="/v1")

TransferDrafter = Annotated[Principal, Depends(route_guard("POST", "/v1/drafts/transfer"))]
ApprovalRequester = Annotated[
    Principal, Depends(route_guard("POST", "/v1/actions/{id}/request-approval"))
]
Canceller = Annotated[Principal, Depends(route_guard("POST", "/v1/actions/{id}/cancel"))]


def _service(request: Request) -> ActionService:
    return request.app.state.actions


@router.post("/drafts/transfer", status_code=status.HTTP_201_CREATED)
def draft_transfer(
    body: TransferDraftRequest, request: Request, principal: TransferDrafter
) -> DraftResult:
    draft = TransferDraft(
        payee=Slot(body.payee_id, Provenance.USER_UTTERANCE),
        amount=Slot(body.amount_fen, Provenance.USER_UTTERANCE),
        account_id=body.account_id,
        remark=body.remark,
    )
    return _service(request).draft_transfer(principal.customer_id, draft)


@router.post("/actions/{id}/request-approval")
def request_approval(
    id: str, request: Request, principal: ApprovalRequester
) -> ApprovalRequestResult:
    """The card becomes visible in the trusted UI; nothing is approved."""
    return _service(request).request_approval(principal.customer_id, id)


@router.post("/actions/{id}/cancel")
def cancel(id: str, request: Request, principal: Canceller) -> CancelResult:
    return _service(request).cancel(principal.customer_id, id)
