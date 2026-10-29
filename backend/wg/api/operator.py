"""Operator routes: locks, unlock, reconciliation and the full audit log (SPEC §16, §19.2)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from wg.api.audit import audit_line
from wg.api.guards import route_guard
from wg.contracts.api import (
    AuditView,
    LocksQuery,
    LocksView,
    LockView,
    OperatorAuditQuery,
    ReconcileRequest,
    ReconcileResult,
    UnlockRequest,
)
from wg.core.k1_session import Principal
from wg.k6.audit import list_audit
from wg.k6.db import Database
from wg.services.operator import OperatorService

router = APIRouter(prefix="/v1/operator")

LockReader = Annotated[Principal, Depends(route_guard("GET", "/v1/operator/locks"))]
Unlocker = Annotated[Principal, Depends(route_guard("POST", "/v1/operator/locks/{id}/unlock"))]
Reconciler = Annotated[Principal, Depends(route_guard("POST", "/v1/operator/reconcile"))]
AuditReader = Annotated[Principal, Depends(route_guard("GET", "/v1/operator/audit"))]


def _service(request: Request) -> OperatorService:
    return request.app.state.operator


@router.get("/locks")
def list_locks(
    request: Request, principal: LockReader, query: Annotated[LocksQuery, Query()]
) -> LocksView:
    return _service(request).locks(query.active_only)


@router.post("/locks/{id}/unlock")
def unlock(id: str, body: UnlockRequest, request: Request, principal: Unlocker) -> LockView:
    return _service(request).unlock(principal.subject_id, id, body.reason)


@router.post("/reconcile")
def reconcile(body: ReconcileRequest, request: Request, principal: Reconciler) -> ReconcileResult:
    return _service(request).reconcile(principal.subject_id, body)


@router.get("/audit")
def operator_audit(
    request: Request, principal: AuditReader, query: Annotated[OperatorAuditQuery, Query()]
) -> AuditView:
    """Every customer's audit rows, or one customer's; `plan_id` keeps the plan and its actions."""
    db: Database = request.app.state.db
    with db.read() as conn:
        refs = None
        if query.plan_id is not None:
            refs = {query.plan_id} | {
                row["logical_action_id"]
                for row in conn.execute(
                    "SELECT DISTINCT logical_action_id FROM actions WHERE plan_id = ?",
                    (query.plan_id,),
                )
            }
        rows = list_audit(conn, customer_id=query.customer_id, refs=refs, limit=query.limit)
    return AuditView(lines=tuple(audit_line(row) for row in rows))
