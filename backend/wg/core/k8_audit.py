"""K8: the breaker, integrity locks and operator unlock (SPEC §16).

`on_failure` is the database's failure policy. It runs inside the unit that records a
failure, so a lock commits together with the evidence that caused it:
- three consecutive unexpected failures of one plan lock that plan; a successful
  submission or settlement resets the count (K6), and expected refusals, transient errors
  and unknown outcomes are never recorded as failures;
- an integrity violation locks at once: an authority incident (a grant whose scope no longer
  matches, a grant used twice) locks the plan, a broken data invariant (ledger sum, a missing
  bank record) locks the customer.

Under a lock, reads, reconciliation, settlement of accepted operations and risk-reducing
writes continue (K3 `check_not_locked`). Only an operator unlocks, with a reason, after every
SUBMITTED and UNKNOWN operation in scope has been reconciled. Unlocking invalidates the open
challenges and grants in scope and resumes nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime

from wg.contracts.enums import LockScope
from wg.contracts.errors import ErrorClass, ErrorCode, WgError
from wg.core import k4_approval as k4
from wg.core.k4_approval import InvalidationReason, StepUp
from wg.k6 import actions, challenges, locks, operations, plans
from wg.k6.db import Reader, RecordedFailure, UnitOfWork
from wg.k6.gateway import reconcile_operation
from wg.k6.locks import Lock
from wg.k6.operations import OperationRow

log = logging.getLogger("wg.k8")

BREAKER_THRESHOLD = 3

# Integrity reasons that concern authority; every other one is a data invariant.
AUTHORITY_REASONS = frozenset({"scope_hash", "grant_reuse", "terms_hash"})


def on_failure(uow: UnitOfWork, failure: RecordedFailure) -> None:
    """Set the lock a recorded failure calls for, if any."""
    if failure.error_class is ErrorClass.UNEXPECTED:
        count = failure.breaker_count
        if failure.plan_id is not None and count is not None and count >= BREAKER_THRESHOLD:
            lock = locks.set_lock(
                uow, LockScope.PLAN, failure.plan_id, f"breaker: {count} unexpected failures"
            )
            log.warning("breaker lock=%s plan=%s", lock.lock_id, failure.plan_id)
        return
    if failure.error_class is not ErrorClass.INTEGRITY:
        return
    reason = failure.detail.get("reason", "unspecified")
    label = f"{ErrorCode.INTEGRITY_VIOLATION.value}: {reason}"[:64]
    if reason in AUTHORITY_REASONS and failure.plan_id is not None:
        lock = locks.set_lock(uow, LockScope.PLAN, failure.plan_id, label)
    else:
        customer_id = _customer_of(uow, failure)
        if customer_id is None:
            lock = locks.set_lock(uow, LockScope.GLOBAL, locks.GLOBAL_SCOPE_REF, label)
        else:
            lock = locks.set_lock(uow, LockScope.CUSTOMER, customer_id, label)
    log.warning("integrity lock=%s scope=%s reason=%s", lock.lock_id, lock.scope, reason)


def _customer_of(reader: Reader, failure: RecordedFailure) -> str | None:
    """The customer a failure concerns, from its detail, operation, action or plan."""
    customer_id = failure.detail.get("customer_id")
    if customer_id is not None:
        return customer_id
    operation_id = failure.detail.get("operation_id")
    if operation_id is not None:
        operation = operations.find_operation(reader, operation_id)
        if operation is not None and operation.customer_id is not None:
            return operation.customer_id
    if failure.logical_action_id is not None:
        action = actions.find_current(reader, failure.logical_action_id)
        if action is not None:
            return action.customer_id
    if failure.plan_id is not None:
        plan = plans.find_plan(reader, failure.plan_id)
        if plan is not None:
            return plan.customer_id
    return None


def operations_in_scope(reader: Reader, lock: Lock) -> list[OperationRow]:
    """SUBMITTED and UNKNOWN operations covered by the lock."""
    if lock.scope is LockScope.PLAN:
        return operations.open_operations(reader, plan_id=lock.scope_ref)
    if lock.scope is LockScope.CUSTOMER:
        return operations.open_operations(reader, customer_id=lock.scope_ref)
    return operations.open_operations(reader)


def unlock(
    uow: UnitOfWork,
    step_up: StepUp,
    *,
    lock_id: str,
    operator_id: str,
    reason: str,
    now: datetime,
) -> Lock:
    """Clear an active lock in the operator's name (SPEC §16).

    Every SUBMITTED and UNKNOWN operation in scope is reconciled first, in this unit; one that
    cannot be (no bank record) raises INTEGRITY_VIOLATION and the lock stays. Open challenges
    and grants in scope are invalidated, so a READY red action waits for a new code. Nothing
    is submitted.
    """
    reason = reason.strip()
    if not reason:
        raise WgError(ErrorCode.UNSUPPORTED_REQUEST, {"fields": ["body.reason"]})
    try:
        lock = locks.get_lock(uow, lock_id)
    except KeyError:
        raise WgError(ErrorCode.NOT_FOUND) from None
    if not lock.active:
        raise WgError(ErrorCode.UNSUPPORTED_REQUEST, {"lock_id": lock_id, "reason": "cleared"})
    for operation in operations_in_scope(uow, lock):
        # The bank's record always resolves UNKNOWN here; without one this raises.
        reconcile_operation(uow, operation.operation_id)
    _invalidate_grants(uow, step_up, lock, now)
    return locks.clear_lock(uow, lock_id, operator_id, reason)


def _invalidate_grants(uow: UnitOfWork, step_up: StepUp, lock: Lock, now: datetime) -> None:
    reason = InvalidationReason.LOCK_CLEARED
    if lock.scope is LockScope.PLAN:
        k4.invalidate_grants(uow, step_up, reason, now, plan_id=lock.scope_ref)
    elif lock.scope is LockScope.CUSTOMER:
        k4.invalidate_grants(uow, step_up, reason, now, customer_id=lock.scope_ref)
    else:
        for customer_id in sorted({c.customer_id for c in challenges.active_challenges(uow)}):
            k4.invalidate_grants(uow, step_up, reason, now, customer_id=customer_id)
