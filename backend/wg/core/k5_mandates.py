"""K5: the direct-debit sweep (S5-SPEC §5.8).

The sweep records what the clock and the customer's answers leave behind: a request nobody
answered by its `respond_by` becomes EXPIRED at that instant, and a payment or decline
action whose request has left AWAITING_CLIENT, or a payment whose agreement has changed
version since its card, is cancelled, so its card shows expired and confirming it is
refused.

It runs after every customer confirm and verify (that customer only), at each step of the
clock walk and at startup. Each item runs in its own unit of work; one that fails is
logged and the others continue. Decisions are made here; every write goes through
`wg.k6`. With no direct-debit rows it reads and writes nothing else.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from wg.contracts.enums import ActionKind, ActionStatus, AuditActor, DebitRequestStatus
from wg.contracts.errors import WgError
from wg.core import k4_approval as k4
from wg.core import k5_coordinator
from wg.core.k4_approval import InvalidationReason, StepUp
from wg.k6 import actions, audit, debit_requests, mandates
from wg.k6.actions import ActionRow
from wg.k6.db import Database, Reader, UnitOfWork
from wg.k6.simtime import ClockError, now
from wg.packs.mandates import OPEN_DECISION_STATUSES

log = logging.getLogger("wg.k5.mandates")

DECISION_KINDS = frozenset({ActionKind.DEBIT_PAY, ActionKind.DEBIT_DECLINE})
REQUEST_CLOSED = "request_closed"
MANDATE_CHANGED = "mandate_changed"


def sweep(db: Database, step_up: StepUp, *, customer_id: str | None = None) -> None:
    """Expire unanswered requests, then close the decision actions they and changes made stale."""
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


def _run(
    db: Database,
    fn: Callable[[UnitOfWork], None],
    name: str,
    *,
    plan_id: str | None = None,
    logical_action_id: str | None = None,
) -> None:
    try:
        db.run_in_uow(fn, "mandate sweep", plan_id=plan_id, logical_action_id=logical_action_id)
    except WgError as error:
        log.warning("sweep step=%s code=%s", name, error.code)
    except Exception:
        log.exception("sweep step=%s failed", name)
