"""Domain event types and their payloads, stored in `domain_events.payload_json`."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import AwareDatetime, NonNegativeInt, PositiveInt, StrictBool

from wg.contracts._base import ContractModel
from wg.contracts.enums import ActionKind, CheckpointResult, Fault, LockScope
from wg.contracts.ids import (
    AccountId,
    ApprovalId,
    CashSnapshotId,
    CommitmentId,
    EventId,
    LockId,
    LogicalActionId,
    OperationId,
    OperatorId,
    PlanId,
    PurposeId,
    ReceiptId,
)
from wg.contracts.money import Fen, NonNegativeFen, Pct, Shares


class DomainEventType(StrEnum):
    COMMITMENTS_CHANGED = "commitments_changed"
    OPERATION_SUBMITTED = "operation_submitted"
    OPERATION_SETTLED = "operation_settled"
    OPERATION_REJECTED = "operation_rejected"
    OPERATION_UNKNOWN = "operation_unknown"
    PLAN_REVISION_CREATED = "plan_revision_created"
    CHECKPOINT_RECORDED = "checkpoint_recorded"
    LOCK_SET = "lock_set"
    LOCK_CLEARED = "lock_cleared"
    CLOCK_ADVANCED = "clock_advanced"
    FAULT_FIRED = "fault_fired"


class CommitmentChange(StrEnum):
    CONFIRMED = "confirmed"
    INCREASED = "increased"
    DECREASED = "decreased"
    PAID = "paid"
    PARTIALLY_PAID = "partially_paid"
    PAID_REPORTED = "paid_reported"
    CANCELLED = "cancelled"
    DISMISSED = "dismissed"


class CommitmentsChanged(ContractModel):
    """One commitment change; commitment_event_id lets consumers count each change once."""

    commitment_event_id: PositiveInt
    commitment_id: CommitmentId
    purpose_id: PurposeId
    change: CommitmentChange
    delta_fen: Fen
    unpaid_fen: NonNegativeFen
    commitments_version: PositiveInt


class _OperationRef(ContractModel):
    operation_id: OperationId
    logical_action_id: LogicalActionId
    revision: PositiveInt
    kind: ActionKind


class OperationSubmitted(_OperationRef):
    account_id: AccountId
    amount_fen: NonNegativeFen | None
    shares: Shares | None
    settle_due_at: AwareDatetime | None


class OperationSettled(_OperationRef):
    receipt_id: ReceiptId
    amount_fen: NonNegativeFen | None
    shares: Shares | None


class OperationRejected(_OperationRef):
    receipt_id: ReceiptId
    reason_code: str


class OperationUnknown(_OperationRef):
    """The outcome is not known locally; reconciliation reads it by idempotency key."""


class PlanRevisionCreated(ContractModel):
    plan_id: PlanId
    logical_action_id: LogicalActionId
    revision: PositiveInt
    previous_revision: PositiveInt
    reason: str
    within_bounds: StrictBool
    trigger_event_id: EventId | None
    cash_snapshot_id: CashSnapshotId
    approval_id: ApprovalId


class CheckpointRecorded(ContractModel):
    plan_id: PlanId
    logical_action_id: LogicalActionId
    result: CheckpointResult
    measured_pct: Pct
    lower_pct: Pct
    upper_pct: Pct
    evaluated_at: AwareDatetime


class LockSet(ContractModel):
    lock_id: LockId
    scope: LockScope
    scope_ref: str
    reason: str


class LockCleared(ContractModel):
    lock_id: LockId
    cleared_by: OperatorId
    clear_reason: str


class ClockAdvanced(ContractModel):
    from_at: AwareDatetime
    to_at: AwareDatetime


class FaultFired(ContractModel):
    fault: Fault
    remaining_count: NonNegativeInt


EVENT_PAYLOADS: Mapping[DomainEventType, type[ContractModel]] = MappingProxyType(
    {
        DomainEventType.COMMITMENTS_CHANGED: CommitmentsChanged,
        DomainEventType.OPERATION_SUBMITTED: OperationSubmitted,
        DomainEventType.OPERATION_SETTLED: OperationSettled,
        DomainEventType.OPERATION_REJECTED: OperationRejected,
        DomainEventType.OPERATION_UNKNOWN: OperationUnknown,
        DomainEventType.PLAN_REVISION_CREATED: PlanRevisionCreated,
        DomainEventType.CHECKPOINT_RECORDED: CheckpointRecorded,
        DomainEventType.LOCK_SET: LockSet,
        DomainEventType.LOCK_CLEARED: LockCleared,
        DomainEventType.CLOCK_ADVANCED: ClockAdvanced,
        DomainEventType.FAULT_FIRED: FaultFired,
    }
)
