"""Read routes of S1: accounts, balance, payees, transfer history, operations, receipts."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from wg.api.guards import route_guard
from wg.contracts.api import (
    AccountsView,
    BalanceView,
    OperationView,
    PayeesQuery,
    PayeesView,
    ReceiptView,
    TransferHistoryQuery,
    TransferHistoryView,
)
from wg.core.k1_session import Principal
from wg.services.reads import ReadService

router = APIRouter(prefix="/v1")

AccountsReader = Annotated[Principal, Depends(route_guard("GET", "/v1/accounts"))]
BalanceReader = Annotated[Principal, Depends(route_guard("GET", "/v1/accounts/{id}/balance"))]
PayeesReader = Annotated[Principal, Depends(route_guard("GET", "/v1/payees"))]
HistoryReader = Annotated[Principal, Depends(route_guard("GET", "/v1/transfers/history"))]
OperationReader = Annotated[Principal, Depends(route_guard("GET", "/v1/operations/{id}"))]
ReceiptReader = Annotated[Principal, Depends(route_guard("GET", "/v1/receipts/{operation_id}"))]


def _service(request: Request) -> ReadService:
    return request.app.state.reads


@router.get("/accounts")
def accounts(request: Request, principal: AccountsReader) -> AccountsView:
    return _service(request).accounts(principal.customer_id)


@router.get("/accounts/{id}/balance")
def balance(id: str, request: Request, principal: BalanceReader) -> BalanceView:
    return _service(request).balance(principal.customer_id, id)


@router.get("/payees")
def payees(
    request: Request, principal: PayeesReader, query: Annotated[PayeesQuery, Query()]
) -> PayeesView:
    return _service(request).payees(principal.customer_id, query.q)


@router.get("/transfers/history")
def transfer_history(
    request: Request, principal: HistoryReader, query: Annotated[TransferHistoryQuery, Query()]
) -> TransferHistoryView:
    return _service(request).history(principal.customer_id, query.days, query.payee_id)


@router.get("/operations/{id}")
def operation(id: str, request: Request, principal: OperationReader) -> OperationView:
    return _service(request).operation(principal.customer_id, id)


@router.get("/receipts/{operation_id}")
def receipt(operation_id: str, request: Request, principal: ReceiptReader) -> ReceiptView:
    return _service(request).receipt(principal.customer_id, operation_id)
