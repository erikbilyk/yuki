"""Read routes of S2: the cash snapshot, bills, spending, transactions, unresolved lines."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from wg.api.guards import route_guard
from wg.contracts.api import (
    BillsQuery,
    BillsView,
    CashSnapshotView,
    SpendingQuery,
    SpendingView,
    TransactionsQuery,
    TransactionsView,
    UnresolvedView,
)
from wg.core.k1_session import Principal
from wg.services.cash import CashService

router = APIRouter(prefix="/v1")

SnapshotReader = Annotated[Principal, Depends(route_guard("GET", "/v1/cash/snapshot"))]
BillsReader = Annotated[Principal, Depends(route_guard("GET", "/v1/bills"))]
SpendingReader = Annotated[Principal, Depends(route_guard("GET", "/v1/spending"))]
TransactionsReader = Annotated[Principal, Depends(route_guard("GET", "/v1/transactions"))]
UnresolvedReader = Annotated[Principal, Depends(route_guard("GET", "/v1/unresolved"))]


def _service(request: Request) -> CashService:
    return request.app.state.cash


@router.get("/cash/snapshot")
def snapshot(request: Request, principal: SnapshotReader) -> CashSnapshotView:
    return _service(request).snapshot(principal.customer_id)


@router.get("/bills")
def bills(
    request: Request, principal: BillsReader, query: Annotated[BillsQuery, Query()]
) -> BillsView:
    return _service(request).bills(principal.customer_id, query)


@router.get("/spending")
def spending(
    request: Request, principal: SpendingReader, query: Annotated[SpendingQuery, Query()]
) -> SpendingView:
    return _service(request).spending(principal.customer_id, query)


@router.get("/transactions")
def transactions(
    request: Request, principal: TransactionsReader, query: Annotated[TransactionsQuery, Query()]
) -> TransactionsView:
    return _service(request).transactions(principal.customer_id, query)


@router.get("/unresolved")
def unresolved(request: Request, principal: UnresolvedReader) -> UnresolvedView:
    return _service(request).unresolved(principal.customer_id)
