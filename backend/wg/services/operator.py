"""The operator's steps: the lock list, reconciliation and unlock (SPEC §16, §19.2).

Reconciliation works under any lock. Each operation is reconciled in its own unit, so one
operation without a bank record (an integrity violation, which K8 turns into a lock) does
not stop the others.
"""

from __future__ import annotations

import logging

from wg.contracts.api import LocksView, LockView, ReconcileRequest, ReconcileResult
from wg.contracts.enums import AuditActor
from wg.contracts.errors import ErrorClass, WgError
from wg.core import k8_audit as k8
from wg.core.k4_approval import StepUp
from wg.k6 import audit, locks, operations
from wg.k6.db import Database
from wg.k6.gateway import Gateway
from wg.k6.locks import Lock
from wg.k6.simtime import now
from wg.services.views import operation_view

log = logging.getLogger("wg.operator")


def lock_view(lock: Lock) -> LockView:
    return LockView(
        lock_id=lock.lock_id,
        scope=lock.scope,
        scope_ref=lock.scope_ref,
        reason=lock.reason[:64],
        since=lock.since,
        cleared_at=lock.cleared_at,
        cleared_by=lock.cleared_by,
        clear_reason=lock.clear_reason,
    )


class OperatorService:
    def __init__(self, db: Database, step_up: StepUp, gateway: Gateway) -> None:
        self.db = db
        self.step_up = step_up
        self.gateway = gateway

    def locks(self, active_only: bool) -> LocksView:
        with self.db.read() as conn:
            rows = locks.list_locks(conn, active_only=active_only)
        return LocksView(locks=tuple(lock_view(lock) for lock in rows))

    def unlock(self, operator_id: str, lock_id: str, reason: str) -> LockView:
        """Clear a lock after reconciling its scope; the reason goes into the audit log."""
        try:
            cleared = self.db.run_in_uow(
                lambda uow: k8.unlock(
                    uow,
                    self.step_up,
                    lock_id=lock_id,
                    operator_id=operator_id,
                    reason=reason,
                    now=now(uow),
                ),
                "operator unlock",
            )
        except WgError as error:
            if error.error_class is ErrorClass.EXPECTED:
                self._audit_refusal(lock_id, error)
            raise
        return lock_view(cleared)

    def _audit_refusal(self, lock_id: str, error: WgError) -> None:
        self.db.run_in_uow(
            lambda uow: audit.record(
                uow,
                AuditActor.OPERATOR,
                "unlock_refused",
                ref=lock_id[:64],
                decision=error.code.value,
            ),
            "audit refusal",
        )

    def reconcile(self, operator_id: str, request: ReconcileRequest) -> ReconcileResult:
        """Reconcile every SUBMITTED and UNKNOWN operation in scope with the bank's record."""
        with self.db.read() as conn:
            pending = operations.open_operations(
                conn, customer_id=request.customer_id, plan_id=request.plan_id
            )
        resolved = 0
        for operation in pending:
            try:
                after = self.gateway.reconcile(operation.operation_id)
            except WgError as error:
                log.warning("reconcile operation=%s code=%s", operation.operation_id, error.code)
                continue
            resolved += int(after.status is not operation.status)
        self.db.run_in_uow(
            lambda uow: audit.record(
                uow,
                AuditActor.OPERATOR,
                "operator_reconcile",
                customer_id=request.customer_id,
                ref=request.plan_id or request.customer_id,
                decision=operator_id,
                reason=f"checked {len(pending)}, resolved {resolved}",
            ),
            "audit reconcile",
        )
        with self.db.read() as conn:
            views = tuple(
                operation_view(conn, operations.get_operation(conn, op.operation_id))
                for op in pending
            )
        return ReconcileResult(checked=len(pending), resolved=resolved, operations=views)
