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
    BillDraftRequest,
    CancelResult,
    CommitmentDraftRequest,
    DraftResult,
    PayBillDraftRequest,
    ReserveDraftRequest,
    ResolutionDraftRequest,
    TransferDraftRequest,
)
from wg.contracts.enums import ActionKind, Provenance
from wg.core.k1_session import Principal
from wg.services.actions import ActionService, PayBillDraft, Slot, TransferDraft

router = APIRouter(prefix="/v1")

TransferDrafter = Annotated[Principal, Depends(route_guard("POST", "/v1/drafts/transfer"))]
BillDrafter = Annotated[Principal, Depends(route_guard("POST", "/v1/drafts/bill"))]
ReserveDrafter = Annotated[Principal, Depends(route_guard("POST", "/v1/drafts/reserve"))]
ResolutionDrafter = Annotated[Principal, Depends(route_guard("POST", "/v1/drafts/resolution"))]
PayBillDrafter = Annotated[Principal, Depends(route_guard("POST", "/v1/drafts/pay-bill"))]
MarkPaidDrafter = Annotated[
    Principal, Depends(route_guard("POST", "/v1/drafts/mark-paid-external"))
]
CancelBillDrafter = Annotated[Principal, Depends(route_guard("POST", "/v1/drafts/cancel-bill"))]
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


@router.post("/drafts/bill", status_code=status.HTTP_201_CREATED)
def draft_bill(body: BillDraftRequest, request: Request, principal: BillDrafter) -> DraftResult:
    return _service(request).draft_bill(principal.customer_id, body)


@router.post("/drafts/reserve", status_code=status.HTTP_201_CREATED)
def draft_reserve(
    body: ReserveDraftRequest, request: Request, principal: ReserveDrafter
) -> DraftResult:
    return _service(request).draft_reserve(principal.customer_id, body.target_fen)


@router.post("/drafts/resolution", status_code=status.HTTP_201_CREATED)
def draft_resolution(
    body: ResolutionDraftRequest, request: Request, principal: ResolutionDrafter
) -> DraftResult:
    return _service(request).draft_resolution(principal.customer_id, body.txn_id, body.category)


@router.post("/drafts/pay-bill", status_code=status.HTTP_201_CREATED)
def draft_pay_bill(
    body: PayBillDraftRequest, request: Request, principal: PayBillDrafter
) -> DraftResult:
    draft = PayBillDraft(
        commitment=Slot(body.commitment_id, Provenance.USER_UTTERANCE),
        amount=None
        if body.amount_fen is None
        else Slot(body.amount_fen, Provenance.USER_UTTERANCE),
        account_id=body.account_id,
    )
    return _service(request).draft_pay_bill(principal.customer_id, draft)


@router.post("/drafts/mark-paid-external", status_code=status.HTTP_201_CREATED)
def draft_mark_paid_external(
    body: CommitmentDraftRequest, request: Request, principal: MarkPaidDrafter
) -> DraftResult:
    return _service(request).draft_bill_action(
        principal.customer_id, ActionKind.MARK_PAID_EXTERNAL, body.commitment_id
    )


@router.post("/drafts/cancel-bill", status_code=status.HTTP_201_CREATED)
def draft_cancel_bill(
    body: CommitmentDraftRequest, request: Request, principal: CancelBillDrafter
) -> DraftResult:
    return _service(request).draft_bill_action(
        principal.customer_id, ActionKind.CANCEL_BILL, body.commitment_id
    )


@router.post("/actions/{id}/request-approval")
def request_approval(
    id: str, request: Request, principal: ApprovalRequester
) -> ApprovalRequestResult:
    """The card becomes visible in the trusted UI; nothing is approved."""
    return _service(request).request_approval(principal.customer_id, id)


@router.post("/actions/{id}/cancel")
def cancel(id: str, request: Request, principal: Canceller) -> CancelResult:
    return _service(request).cancel(principal.customer_id, id)
