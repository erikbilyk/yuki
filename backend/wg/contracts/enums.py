"""State and classification enums (SPEC §7, §8.1, §8.2, §12.7)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class ActionStatus(StrEnum):
    DRAFT = "DRAFT"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AWAITING_STEPUP = "AWAITING_STEPUP"
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    SETTLED = "SETTLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    PAUSED = "PAUSED"
    SUPERSEDED = "SUPERSEDED"


_S = ActionStatus

TERMINAL_ACTION_STATUSES: frozenset[ActionStatus] = frozenset(
    {_S.SETTLED, _S.REJECTED, _S.CANCELLED, _S.SUPERSEDED}
)

# Allowed status changes of one action row (SPEC §8.1). A PAUSED action is never
# resumed in place: a new revision starts in DRAFT and the paused row becomes SUPERSEDED.
ACTION_TRANSITIONS: Mapping[ActionStatus, frozenset[ActionStatus]] = MappingProxyType(
    {
        _S.DRAFT: frozenset(
            {
                _S.AWAITING_APPROVAL,
                _S.AWAITING_STEPUP,
                _S.READY,
                _S.SETTLED,
                _S.CANCELLED,
                _S.PAUSED,
                _S.SUPERSEDED,
            }
        ),
        _S.AWAITING_APPROVAL: frozenset({_S.READY, _S.AWAITING_STEPUP, _S.CANCELLED}),
        _S.AWAITING_STEPUP: frozenset({_S.READY, _S.CANCELLED, _S.PAUSED, _S.SUPERSEDED}),
        _S.READY: frozenset(
            {
                _S.SUBMITTED,
                _S.SETTLED,
                _S.REJECTED,
                _S.AWAITING_STEPUP,
                _S.UNKNOWN,
                _S.CANCELLED,
                _S.PAUSED,
                _S.SUPERSEDED,
            }
        ),
        _S.SUBMITTED: frozenset({_S.SETTLED, _S.REJECTED, _S.UNKNOWN}),
        _S.UNKNOWN: frozenset({_S.SUBMITTED, _S.SETTLED, _S.REJECTED}),
        _S.PAUSED: frozenset({_S.CANCELLED, _S.SUPERSEDED}),
        _S.SETTLED: frozenset(),
        _S.REJECTED: frozenset(),
        _S.CANCELLED: frozenset(),
        _S.SUPERSEDED: frozenset(),
    }
)


class PlanStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    LOCKED = "locked"
    COMPLETED = "completed"
    PARTIAL = "partial"


class CheckpointResult(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    CHECKPOINT_MISSED = "checkpoint_missed"


class OperationStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    SETTLED = "SETTLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ReceiptStatus(StrEnum):
    SETTLED = "SETTLED"
    REJECTED = "REJECTED"


class CommitmentStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    PAID_REPORTED = "PAID_REPORTED"
    CANCELLED = "CANCELLED"
    DISMISSED = "DISMISSED"


class BillStatus(StrEnum):
    """Status of a bill derived inside a cash snapshot (SPEC §13.2)."""

    SECURED = "secured"
    UNSECURED = "unsecured"
    OVERDUE = "overdue"
    PAID = "paid"
    PAID_REPORTED = "paid_reported"


class Tier(StrEnum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    FORBIDDEN = "FORBIDDEN"


class Provenance(StrEnum):
    USER_UTTERANCE = "user_utterance"
    SERVER_RECORD = "server_record"
    UNTRUSTED_TEXT = "untrusted_text"


class Disposition(StrEnum):
    EXECUTED = "executed"
    CLARIFICATION = "clarification"
    INFEASIBLE = "infeasible"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"
    NO_ACTION = "no_action"


class ActionKind(StrEnum):
    TRANSFER = "TRANSFER"
    PAY_BILL = "PAY_BILL"
    DESIGNATE_RESERVE = "DESIGNATE_RESERVE"
    CONFIRM_BILL = "CONFIRM_BILL"
    RESOLVE_ITEM = "RESOLVE_ITEM"
    CANCEL_BILL = "CANCEL_BILL"
    MARK_PAID_EXTERNAL = "MARK_PAID_EXTERNAL"
    REDEEM = "REDEEM"
    PURCHASE = "PURCHASE"
    CHECK_OUTCOME = "CHECK_OUTCOME"
    ADD_PAYEE = "ADD_PAYEE"


class LockScope(StrEnum):
    PLAN = "plan"
    CUSTOMER = "customer"
    GLOBAL = "global"


class Role(StrEnum):
    CUSTOMER = "customer"
    OPERATOR = "operator"
    DEMO = "demo"


class Channel(StrEnum):
    """Where an agent turn comes from: the web app or the IM channel (SPEC §21)."""

    WEB = "web"
    IM = "im"


class PlanKind(StrEnum):
    SINGLE = "single"
    REBALANCE = "rebalance"
    MIXED = "mixed"


class PlanMode(StrEnum):
    COORDINATED = "coordinated"
    FLAT = "flat"


class ApprovalScopeKind(StrEnum):
    ACTION = "action"
    PLAN = "plan"


class OperationEventKind(StrEnum):
    SUBMITTED = "SUBMITTED"
    SETTLED = "SETTLED"
    REJECTED = "REJECTED"
    UNKNOWN_OBSERVED = "UNKNOWN_OBSERVED"
    RECONCILED = "RECONCILED"


class ReservationState(StrEnum):
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


class BankRequestStatus(StrEnum):
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"


class AuditActor(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    COORDINATOR = "coordinator"
    OPERATOR = "operator"
    SYSTEM = "system"


class PayeeBank(StrEnum):
    """`same` is shown as 本行, `other` as 他行."""

    SAME = "same"
    OTHER = "other"


class Recurrence(StrEnum):
    MONTHLY = "monthly"
    NONE = "none"


class Fault(StrEnum):
    """Fault points armed through /sim/faults (SPEC §12.7)."""

    CRASH_BEFORE_COMMIT = "crash_before_commit"
    CRASH_AFTER_COMMIT = "crash_after_commit"
    PAUSE_BEFORE_COMMIT = "pause_before_commit"
    PAUSE_AFTER_COMMIT = "pause_after_commit"
    DROP_RESPONSE = "drop_response"
    BANK_ACK_LOST = "bank_ack_lost"
    SETTLEMENT_REJECT = "settlement_reject"
    PROBE_TIMEOUT = "probe_timeout"
    BUSY = "busy"
    UNEXPECTED = "unexpected"
    TICK_CRASH = "tick_crash"
