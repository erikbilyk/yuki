"""K6 gateway: executes a READY action exactly once, in one unit of work (SPEC §12).

Entry points:
- `Gateway.submit(customer_id, logical_action_id)`: SPEC §12.3. Idempotency key, authority
  (approval, then the grant when the recomputed tier is RED), the pack's prechecks, the
  reservation, the operation with its SUBMITTED event, the bank call and, for a payment that
  settles at once, the settlement and its receipt. A repeat returns the original operation.
- `Gateway.settle_due()` and `settle_due(uow, now)`: settlements due at or before now (§12.4).
- `Gateway.reconcile(operation_id)` and `reconcile_operation(uow, ...)`: resolve a SUBMITTED or
  UNKNOWN operation from the bank's record under the same key (§12.6).

Fault points of `submit` (SPEC §12.7):
- `busy`: at the start of an attempt of the unit, as a forced SQLITE_BUSY; the unit is
  retried whole. Each armed count forces one busy attempt.
- `unexpected`: after the operation row is written, before the bank call; the unit rolls back.
- `crash_before_commit`, `pause_before_commit`: after every write of the unit, before COMMIT.
- `crash_after_commit`, `pause_after_commit`: after COMMIT, before the response.
- `drop_response`: after COMMIT; the caller gets `ResponseDropped` and sends no body.
- `bank_ack_lost`, `settlement_reject`: inside the bank simulator (`wg.k6.bank`).
The four that fire before COMMIT are taken in a unit of their own when a new submission
starts, so neither the rollback nor the process exit they cause re-arms them. The others
are consumed in the submitting unit and commit with it.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from wg.contracts.enums import (
    ActionStatus,
    AuditActor,
    Fault,
    OperationEventKind,
    OperationStatus,
    ReceiptStatus,
    ReservationState,
    Tier,
)
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.events import (
    DomainEventType,
    OperationRejected,
    OperationSettled,
    OperationSubmitted,
    OperationUnknown,
)
from wg.core import k4_approval as k4
from wg.core.k1_session import owned
from wg.core.k3_policy import check_not_locked
from wg.k6 import actions, audit, events, faults, operations
from wg.k6.actions import ActionRow
from wg.k6.bank import AckLost, BankRequest, find_request
from wg.k6.db import Database, Reader, UnitOfWork
from wg.k6.operations import OperationRow
from wg.k6.simtime import now
from wg.packs.base import ActionPack, OperationTerms
from wg.packs.registry import pack_for

log = logging.getLogger("wg.k6.gateway")

CRASH_EXIT_STATUS = 137

# Faults that fire before the unit commits; each is taken in its own unit first.
_TAKEN_FIRST = (
    Fault.BUSY,
    Fault.UNEXPECTED,
    Fault.CRASH_BEFORE_COMMIT,
    Fault.PAUSE_BEFORE_COMMIT,
)
_AFTER_COMMIT = (Fault.CRASH_AFTER_COMMIT, Fault.PAUSE_AFTER_COMMIT, Fault.DROP_RESPONSE)


class ResponseDropped(Exception):
    """`drop_response` fired: the submission committed, and the response must not be sent."""

    def __init__(self, operation: OperationRow) -> None:
        super().__init__(operation.operation_id)
        self.operation = operation


class FaultFailure(RuntimeError):
    """The `unexpected` fault: an exception inside execution that nothing anticipated."""


@dataclass(frozen=True)
class Submission:
    operation: OperationRow
    created: bool


@dataclass
class _Armed:
    """Faults taken for one submission before its unit opened."""

    busy: int = 0
    unexpected: bool = False
    crash_before: bool = False
    pause_before: bool = False
    after_commit: set[Fault] = field(default_factory=set)


def wait_for_kill(marker: Path) -> NoReturn:
    """Write the marker the test harness waits for, then block until SIGKILL."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(os.getpid()), encoding="utf-8")
    while True:
        time.sleep(60)


def forced_busy() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked (fault: busy)")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY  # type: ignore[attr-defined]
    return error


class Gateway:
    def __init__(
        self,
        db: Database,
        *,
        fault_wait_file: Path,
        exit_process: Callable[[int], object] = os._exit,
        pause: Callable[[Path], object] = wait_for_kill,
    ) -> None:
        self.db = db
        self.fault_wait_file = fault_wait_file
        self._exit = exit_process
        self._pause = pause

    # Submission (SPEC §12.3)

    def submit(self, customer_id: str, logical_action_id: str) -> Submission:
        """Execute the action's current revision once; a repeat returns the original operation."""
        with self.db.read() as conn:
            action = owned(actions.find_current(conn, logical_action_id), customer_id, _owner)
            key = operations.idempotency_key(action.logical_action_id, action.revision)
            repeat = operations.find_by_key(conn, key) is not None
        armed = _Armed() if repeat else self._take_faults_first()

        def run(uow: UnitOfWork) -> Submission:
            if armed.busy:
                armed.busy -= 1
                raise forced_busy()
            submission = self._submit_in(uow, customer_id, logical_action_id, armed)
            if submission.created:
                armed.after_commit = {f for f in _AFTER_COMMIT if faults.consume(uow, f)}
                self._before_commit(armed)
            return submission

        try:
            submission = self.db.run_in_uow(
                run,
                "gateway submit",
                plan_id=action.plan_id,
                logical_action_id=logical_action_id,
            )
        except WgError as error:
            if error.code is ErrorCode.TIER_CHANGED:
                self._await_step_up(customer_id, logical_action_id)
            raise
        except Exception as exc:
            # run_in_uow rolled back and recorded the failure; the caller gets its error code.
            log.exception("submission failed action=%s", logical_action_id)
            raise WgError(
                ErrorCode.UNEXPECTED_FAILURE, {"logical_action_id": logical_action_id}
            ) from exc
        self._after_commit(armed, submission)
        return submission

    def _submit_in(
        self, uow: UnitOfWork, customer_id: str, logical_action_id: str, armed: _Armed
    ) -> Submission:
        at = now(uow)
        action = owned(actions.find_current(uow, logical_action_id), customer_id, _owner)
        key = operations.idempotency_key(action.logical_action_id, action.revision)
        existing = operations.find_by_key(uow, key)
        if existing is not None:
            if existing.request_hash != action.request_hash:
                raise WgError(
                    ErrorCode.IDEMPOTENCY_MISMATCH, {"logical_action_id": logical_action_id}
                )
            return Submission(existing, created=False)
        if action.status is ActionStatus.AWAITING_STEPUP:
            raise WgError(ErrorCode.APPROVAL_REQUIRED, {"logical_action_id": logical_action_id})
        if action.status is not ActionStatus.READY:
            raise k4.state_refusal(action).error()
        check_not_locked(uow, customer_id, action.plan_id)

        pack = pack_for(action.kind)
        self._authorize(uow, pack, action, at)
        reasons = pack.precheck(uow, customer_id, action.kind, action.params, at)
        if reasons:
            raise reasons[0].error()

        terms = pack.operation_terms(uow, action, at)
        operation = self._insert_operation(uow, action, terms, at)
        pack.reserve(uow, operation, terms)
        if armed.unexpected:
            raise FaultFailure("fault: unexpected")
        try:
            bank_request = pack.apply(uow, operation, terms, at)
        except AckLost:
            operation = _mark_unknown(uow, operation)
        else:
            operation = _record_outcome(uow, pack, operation, bank_request)
        return Submission(operation, created=True)

    def _authorize(
        self, uow: UnitOfWork, pack: ActionPack, action: ActionRow, at: datetime
    ) -> None:
        """A valid approval for every tier; a grant when the action or its recomputed tier is RED.

        A YELLOW action whose day total has since risen to RED has no grant; it is refused with
        TIER_CHANGED and waits for a code (SPEC §10.2, T6).
        """
        k4.valid_approval(uow, action, at)
        tier = pack.tier(uow, action.customer_id, action.kind, action.params, at)
        if action.tier is not Tier.RED and tier is Tier.RED:
            if k4.find_grant(uow, action) is None:
                raise WgError(
                    ErrorCode.TIER_CHANGED,
                    {"logical_action_id": action.logical_action_id, "tier": tier.value},
                )
        if Tier.RED in (action.tier, tier):
            k4.consume_grant(uow, action, at)

    def _insert_operation(
        self, uow: UnitOfWork, action: ActionRow, terms: OperationTerms, at: datetime
    ) -> OperationRow:
        operation = operations.insert_operation(
            uow,
            key=operations.idempotency_key(action.logical_action_id, action.revision),
            request_hash=action.request_hash,
            kind=action.kind.value,
            submitted_at=at,
            logical_action_id=action.logical_action_id,
            revision=action.revision,
            customer_id=action.customer_id,
            account_id=terms.account_id,
            amount_fen=terms.amount_fen,
            shares=terms.shares,
            product_id=terms.product_id,
            payee_id=terms.payee_id,
            commitment_id=terms.commitment_id,
            settle_due_at=terms.settle_due_at,
        )
        operations.append_event(uow, operation.operation_id, OperationEventKind.SUBMITTED)
        actions.set_status(uow, action, ActionStatus.SUBMITTED)
        events.append_event(
            uow,
            DomainEventType.OPERATION_SUBMITTED,
            OperationSubmitted(
                **_ref(operation),
                account_id=terms.account_id,
                amount_fen=terms.amount_fen,
                shares=terms.shares,
                settle_due_at=terms.settle_due_at,
            ),
            customer_id=action.customer_id,
            plan_id=action.plan_id,
        )
        audit.record(
            uow,
            AuditActor.SYSTEM,
            "operation_submitted",
            customer_id=action.customer_id,
            ref=operation.operation_id,
            decision=action.tier.value,
        )
        return operation

    def _take_faults_first(self) -> _Armed:
        with self.db.read() as conn:
            armed_now = {f.fault: f.remaining_count for f in faults.armed_faults(conn)}
        if not any(fault in armed_now for fault in _TAKEN_FIRST):
            return _Armed()
        armed = _Armed()
        for _ in range(min(armed_now.get(Fault.BUSY, 0), 4)):
            armed.busy += int(faults.take(self.db, Fault.BUSY))
        armed.unexpected = Fault.UNEXPECTED in armed_now and faults.take(self.db, Fault.UNEXPECTED)
        armed.crash_before = Fault.CRASH_BEFORE_COMMIT in armed_now and faults.take(
            self.db, Fault.CRASH_BEFORE_COMMIT
        )
        armed.pause_before = Fault.PAUSE_BEFORE_COMMIT in armed_now and faults.take(
            self.db, Fault.PAUSE_BEFORE_COMMIT
        )
        return armed

    def _before_commit(self, armed: _Armed) -> None:
        if armed.crash_before:
            log.warning("fault fired: crash_before_commit")
            self._exit(CRASH_EXIT_STATUS)
        if armed.pause_before:
            log.warning("fault fired: pause_before_commit")
            self._pause(self.fault_wait_file)

    def _after_commit(self, armed: _Armed, submission: Submission) -> None:
        if Fault.CRASH_AFTER_COMMIT in armed.after_commit:
            log.warning("fault fired: crash_after_commit")
            self._exit(CRASH_EXIT_STATUS)
        if Fault.PAUSE_AFTER_COMMIT in armed.after_commit:
            log.warning("fault fired: pause_after_commit")
            self._pause(self.fault_wait_file)
        if Fault.DROP_RESPONSE in armed.after_commit:
            raise ResponseDropped(submission.operation)

    def _await_step_up(self, customer_id: str, logical_action_id: str) -> None:
        """After TIER_CHANGED the READY action waits for a code (READY → AWAITING_STEPUP)."""

        def move(uow: UnitOfWork) -> None:
            action = owned(actions.find_current(uow, logical_action_id), customer_id, _owner)
            if action.status is ActionStatus.READY:
                actions.set_status(uow, action, ActionStatus.AWAITING_STEPUP)
                audit.record(
                    uow,
                    AuditActor.SYSTEM,
                    "tier_changed",
                    customer_id=customer_id,
                    ref=logical_action_id,
                    decision=Tier.RED.value,
                )

        self.db.run_in_uow(move, "await step-up", logical_action_id=logical_action_id)

    # Settlement and reconciliation

    def settle_due(self) -> list[OperationRow]:
        """Apply every settlement due at the simulated now, each in its own unit of work.

        An operation that fails is recorded by `run_in_uow` (K8 may lock its scope) and the
        others still settle; the result lists the operations that were processed.
        """
        with self.db.read() as conn:
            due = [
                (op, _action_of(conn, op).plan_id)
                for op in operations.due_operations(conn, now(conn))
            ]
        settled = []
        for operation, plan_id in due:
            try:
                settled.append(
                    self.db.run_in_uow(
                        lambda uow, op=operation: settle_operation(uow, op.operation_id, now(uow)),
                        "settle operation",
                        plan_id=plan_id,
                        logical_action_id=operation.logical_action_id,
                    )
                )
            except WgError as error:
                log.warning(
                    "settlement refused operation=%s code=%s", operation.operation_id, error.code
                )
        return settled

    def reconcile(self, operation_id: str) -> OperationRow:
        return self.db.run_in_uow(
            lambda uow: reconcile_operation(uow, operation_id), "reconcile operation"
        )


def _owner(action: ActionRow) -> str:
    return action.customer_id


def _ref(operation: OperationRow) -> dict[str, object]:
    assert operation.logical_action_id is not None and operation.revision is not None
    return {
        "operation_id": operation.operation_id,
        "logical_action_id": operation.logical_action_id,
        "revision": operation.revision,
        "kind": operation.action_kind,
    }


def _action_of(reader: Reader, operation: OperationRow) -> ActionRow:
    assert operation.logical_action_id is not None and operation.revision is not None
    return actions.get_revision(reader, operation.logical_action_id, operation.revision)


def _record_outcome(
    uow: UnitOfWork, pack: ActionPack, operation: OperationRow, bank_request: BankRequest
) -> OperationRow:
    """Bring the operation in line with the bank's record: rejected, settled or still pending."""
    if bank_request.rejected:
        return _finish(uow, pack, operation, bank_request, ReceiptStatus.REJECTED)
    if bank_request.settled:
        return _finish(uow, pack, operation, bank_request, ReceiptStatus.SETTLED)
    return operation


def _finish(
    uow: UnitOfWork,
    pack: ActionPack,
    operation: OperationRow,
    bank_request: BankRequest,
    outcome: ReceiptStatus,
) -> OperationRow:
    """The terminal outcome: reservation, operation, action, the one receipt, event and audit."""
    settled = outcome is ReceiptStatus.SETTLED
    reservation = operations.reservation_of(uow, operation.operation_id)
    if reservation is not None:
        state = ReservationState.CONSUMED if settled else ReservationState.RELEASED
        operations.close_reservation(uow, reservation, state)
    action = _action_of(uow, operation)
    approval = k4.approval_for(uow, action)
    reason = None if settled else str(bank_request.effect.get("reason") or "rejected")
    operation = operations.set_status(
        uow,
        operation,
        OperationStatus.SETTLED if settled else OperationStatus.REJECTED,
        settled_at=bank_request.settled_at if settled else None,
    )
    operations.append_event(
        uow,
        operation.operation_id,
        OperationEventKind.SETTLED if settled else OperationEventKind.REJECTED,
        reason,
    )
    receipt = operations.insert_receipt(
        uow,
        operation_id=operation.operation_id,
        status=outcome,
        effect=pack.receipt_effect(operation, bank_request),
        terms_hash=approval.terms_hash if approval else None,
    )
    actions.set_status(uow, action, ActionStatus.SETTLED if settled else ActionStatus.REJECTED)
    if settled:
        payload = OperationSettled(
            **_ref(operation),
            receipt_id=receipt.receipt_id,
            amount_fen=operation.amount_fen,
            shares=operation.shares,
        )
        event_type = DomainEventType.OPERATION_SETTLED
    else:
        payload = OperationRejected(
            **_ref(operation), receipt_id=receipt.receipt_id, reason_code=reason or "rejected"
        )
        event_type = DomainEventType.OPERATION_REJECTED
    events.append_event(
        uow, event_type, payload, customer_id=operation.customer_id, plan_id=action.plan_id
    )
    audit.record(
        uow,
        AuditActor.SYSTEM,
        "operation_settled" if settled else "operation_rejected",
        customer_id=operation.customer_id,
        ref=operation.operation_id,
        reason=reason,
    )
    return operation


def _mark_unknown(uow: UnitOfWork, operation: OperationRow) -> OperationRow:
    """The bank's acknowledgement was lost: the outcome is not known here (SPEC §12.6)."""
    action = _action_of(uow, operation)
    operation = operations.set_status(uow, operation, OperationStatus.UNKNOWN)
    operations.append_event(
        uow, operation.operation_id, OperationEventKind.UNKNOWN_OBSERVED, "bank_ack_lost"
    )
    actions.set_status(uow, action, ActionStatus.UNKNOWN)
    events.append_event(
        uow,
        DomainEventType.OPERATION_UNKNOWN,
        OperationUnknown(**_ref(operation)),
        customer_id=operation.customer_id,
        plan_id=action.plan_id,
    )
    audit.record(
        uow,
        AuditActor.SYSTEM,
        "operation_unknown",
        customer_id=operation.customer_id,
        ref=operation.operation_id,
        reason="bank_ack_lost",
    )
    return operation


def reconcile_operation(uow: UnitOfWork, operation_id: str) -> OperationRow:
    """Resolve a SUBMITTED or UNKNOWN operation from the bank's record under its own key.

    The original effect and the one receipt are kept; no new key is ever used. An operation
    the bank has no record of cannot exist here, since both are written in one unit, so it is
    an integrity violation (SPEC §12.6).
    """
    operation = operations.get_operation(uow, operation_id)
    if operation.status not in (OperationStatus.SUBMITTED, OperationStatus.UNKNOWN):
        return operation
    bank_request = find_request(uow, operation.idempotency_key)
    if bank_request is None:
        raise WgError(
            ErrorCode.INTEGRITY_VIOLATION,
            {"operation_id": operation_id, "reason": "no_bank_record"},
        )
    pack = pack_for(operation.action_kind)
    if operation.status is OperationStatus.UNKNOWN:
        operations.append_event(uow, operation_id, OperationEventKind.RECONCILED)
        if not (bank_request.rejected or bank_request.settled):
            operation = operations.set_status(uow, operation, OperationStatus.SUBMITTED)
            actions.set_status(uow, _action_of(uow, operation), ActionStatus.SUBMITTED)
            audit.record(
                uow,
                AuditActor.SYSTEM,
                "operation_reconciled",
                customer_id=operation.customer_id,
                ref=operation_id,
                decision=OperationStatus.SUBMITTED.value,
            )
            return operation
    return _record_outcome(uow, pack, operation, bank_request)


def settle_operation(uow: UnitOfWork, operation_id: str, at: datetime) -> OperationRow:
    """Settle one operation whose due time has come; settling twice changes nothing (T56).

    An UNKNOWN operation is reconciled first; only a SUBMITTED one goes to the bank.
    """
    operation = reconcile_operation(uow, operation_id)
    if operation.status is not OperationStatus.SUBMITTED:
        return operation
    if operation.settle_due_at is None or operation.settle_due_at > at:
        return operation
    pack = pack_for(operation.action_kind)
    bank_request = pack.settle(uow, operation, operation.settle_due_at)
    return _record_outcome(uow, pack, operation, bank_request)


def settle_due(uow: UnitOfWork, at: datetime) -> list[OperationRow]:
    """Settle, inside `uow`, every operation due at or before `at`, in due order."""
    return [
        settle_operation(uow, operation.operation_id, at)
        for operation in operations.due_operations(uow, at)
    ]
