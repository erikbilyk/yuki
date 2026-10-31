"""K5: the direct-debit sweep (S5-SPEC §5.8).

The sweep records what the clock and the customer's answers leave behind: a request nobody
answered by its `respond_by` becomes EXPIRED at that instant, and a payment or decline
action whose request has left AWAITING_CLIENT, or a payment whose agreement has changed
version since its card, is cancelled, so its card shows expired and confirming it is
refused. Finally each settled cancel, suspend or cap lowering is checked by a probe: a
merchant request through the normal intake that the change must refuse (S5-SPEC §5.7).
A probe that is accepted is an integrity incident and locks the customer.

It runs after every customer confirm and verify (that customer only), at each step of the
clock walk and at startup. Each item runs in its own unit of work; one that fails is
logged and the others continue. Decisions are made here; every write goes through
`wg.k6`. With no direct-debit rows it reads and writes nothing else.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from wg.contracts.enums import (
    ActionKind,
    ActionStatus,
    AuditActor,
    DebitRequestStatus,
    EffectCheck,
    Fault,
    OperationStatus,
)
from wg.contracts.errors import ErrorCode, WgError
from wg.core import k4_approval as k4
from wg.core import k5_coordinator, mandate_rules
from wg.core.k4_approval import InvalidationReason, StepUp
from wg.k6 import actions, audit, debit_requests, faults, mandates, operations
from wg.k6.actions import ActionRow
from wg.k6.db import Database, Reader, UnitOfWork
from wg.k6.debit_requests import IncomingDebit
from wg.k6.simtime import ClockError, now
from wg.packs.mandates import OPEN_DECISION_STATUSES

log = logging.getLogger("wg.k5.mandates")

DECISION_KINDS = frozenset({ActionKind.DEBIT_PAY, ActionKind.DEBIT_DECLINE})
REQUEST_CLOSED = "request_closed"
MANDATE_CHANGED = "mandate_changed"
PROBE_ACCEPTED = "probe_accepted"


def sweep(db: Database, step_up: StepUp, *, customer_id: str | None = None) -> None:
    """Expire unanswered requests, close the decision actions made stale, probe changes."""
    with db.read() as conn:
        try:
            at = now(conn)
        except ClockError:
            return
        overdue = debit_requests.overdue_requests(conn, at, customer_id)
    for request in overdue:
        _run(
            db,
            lambda uow, request_id=request.request_id: _expire(uow, request_id),
            f"expire {request.request_id}",
        )
    with db.read() as conn:
        stale = [
            action
            for action in actions.actions_of_kinds(
                conn, DECISION_KINDS, OPEN_DECISION_STATUSES, customer_id
            )
            if stale_reason(conn, action) is not None
        ]
    for action in stale:
        _run(
            db,
            lambda uow, a=action: _close_stale(uow, step_up, a),
            f"close {action.logical_action_id}",
            plan_id=action.plan_id,
            logical_action_id=action.logical_action_id,
        )
    with db.read() as conn:
        unverified = [
            (operation_id, operations.get_operation(conn, operation_id))
            for operation_id in mandates.unverified_changes(
                conn, [kind.value for kind in mandate_rules.PROBE_CHECKS], customer_id
            )
        ]
    for operation_id, operation in unverified:
        plan_id = _plan_of(db, operation)
        accepted = _run(
            db,
            lambda uow, op=operation_id: _probe(uow, op),
            f"probe {operation_id}",
            plan_id=plan_id,
            logical_action_id=operation.logical_action_id,
        )
        if accepted is not None:
            _run(
                db,
                lambda uow, detail=accepted: _raise_probe_accepted(detail),
                f"probe accepted {operation_id}",
                plan_id=plan_id,
                logical_action_id=operation.logical_action_id,
            )


def stale_reason(reader: Reader, action: ActionRow) -> str | None:
    """Why an open payment or decline can no longer be confirmed, or None."""
    request = debit_requests.find_request(reader, str(action.params["request_id"]))
    if request is None or request.status is not DebitRequestStatus.AWAITING_CLIENT:
        return REQUEST_CLOSED
    if action.kind is ActionKind.DEBIT_PAY:
        mandate = mandates.find_mandate(reader, str(action.params["mandate_ref"]))
        if mandate is None or mandate.version != action.params["mandate_version"]:
            return MANDATE_CHANGED
    return None


def _expire(uow: UnitOfWork, request_id: str) -> None:
    request = debit_requests.find_request(uow, request_id)
    if (
        request is not None
        and not request.probe
        and request.status is DebitRequestStatus.AWAITING_CLIENT
        and request.respond_by <= now(uow)
    ):
        debit_requests.expire(uow, request_id)


def _probe(uow: UnitOfWork, operation_id: str) -> dict[str, str] | None:
    """One attempt to check a settled change; the detail of an incident if the probe got in.

    An armed `probe_timeout` is consumed instead of probing and leaves the check pending, to
    be retried by the next sweep.
    """
    operation = operations.get_operation(uow, operation_id)
    newest = mandates.newest_effect_event(uow, operation_id)
    if operation.status is not OperationStatus.SETTLED or (
        newest is not None and newest.result is not EffectCheck.PENDING
    ):
        return None
    check_kind = mandate_rules.PROBE_CHECKS[operation.action_kind]
    if faults.consume(uow, Fault.PROBE_TIMEOUT):
        mandates.append_effect_event(
            uow, operation_id, check_kind, EffectCheck.PENDING, reason=Fault.PROBE_TIMEOUT.value
        )
        return None
    receipt = operations.receipt_of(uow, operation_id)
    assert receipt is not None
    mandate = mandates.find_mandate(uow, str(receipt.effect["mandate_ref"]))
    assert mandate is not None
    amount = mandate_rules.probe_amount(
        operation.action_kind, mandate, debit_requests.mandate_requests(uow, mandate.mandate_ref)
    )
    attempt = mandates.effect_event_count(uow, operation_id) + 1
    probe = debit_requests.receive_in(
        uow,
        IncomingDebit(
            merchant_id=mandate.merchant_id,
            merchant_request_id=mandate_rules.probe_request_id(operation_id, attempt),
            mandate_ref=mandate.mandate_ref,
            amount_fen=amount,
            currency="CNY",
        ),
        probe=True,
    ).request
    audit.record(
        uow,
        AuditActor.COORDINATOR,
        "probe_sent",
        customer_id=mandate.customer_id,
        ref=probe.request_id,
        decision=check_kind,
    )
    refused = probe.status in mandate_rules.REFUSED_STATUSES
    mandates.append_effect_event(
        uow,
        operation_id,
        check_kind,
        EffectCheck.VERIFIED if refused else EffectCheck.FAILED,
        probe_request_id=probe.request_id,
    )
    audit.record(
        uow,
        AuditActor.COORDINATOR,
        "effect_verified" if refused else "effect_check_failed",
        customer_id=mandate.customer_id,
        ref=operation_id,
        decision=probe.status.value,
    )
    if refused:
        return None
    return {
        "reason": PROBE_ACCEPTED,
        "customer_id": mandate.customer_id,
        "operation_id": operation_id,
        "mandate_ref": mandate.mandate_ref,
    }


def _raise_probe_accepted(detail: dict[str, str]) -> None:
    """Report an accepted probe through the failure path, which locks the customer (K8)."""
    raise WgError(ErrorCode.INTEGRITY_VIOLATION, dict(detail))


def _plan_of(db: Database, operation: operations.OperationRow) -> str | None:
    if operation.logical_action_id is None or operation.revision is None:
        return None
    with db.read() as conn:
        return actions.get_revision(conn, operation.logical_action_id, operation.revision).plan_id


def _close_stale(uow: UnitOfWork, step_up: StepUp, action: ActionRow) -> None:
    """Cancel the action as the customer's cancel would, if it is still open and stale."""
    current = actions.get_revision(uow, action.logical_action_id, action.revision)
    if current.status not in OPEN_DECISION_STATUSES:
        return
    reason = stale_reason(uow, current)
    if reason is None:
        return
    k4.invalidate_grants(
        uow,
        step_up,
        InvalidationReason.CANCELLED,
        now(uow),
        logical_action_id=current.logical_action_id,
    )
    current = actions.get_revision(uow, current.logical_action_id, current.revision)
    actions.set_status(uow, current, ActionStatus.CANCELLED)
    audit.record(
        uow,
        AuditActor.COORDINATOR,
        "action_cancelled",
        customer_id=current.customer_id,
        ref=current.logical_action_id,
        decision=reason,
    )
    if current.plan_id is not None:
        k5_coordinator.store_status(uow, current.plan_id)


def _run[T](
    db: Database,
    fn: Callable[[UnitOfWork], T],
    name: str,
    *,
    plan_id: str | None = None,
    logical_action_id: str | None = None,
) -> T | None:
    """Run one item in its own unit; a failure is recorded by `run_in_uow` and logged here."""
    try:
        return db.run_in_uow(
            fn, "mandate sweep", plan_id=plan_id, logical_action_id=logical_action_id
        )
    except WgError as error:
        log.warning("sweep step=%s code=%s", name, error.code)
    except Exception:
        log.exception("sweep step=%s failed", name)
    return None
