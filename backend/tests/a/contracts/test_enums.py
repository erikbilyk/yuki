import pytest

from wg.contracts import enums
from wg.contracts.enums import ACTION_TRANSITIONS, TERMINAL_ACTION_STATUSES, ActionStatus

# Member lists as written in SPEC §8.1, §8.2, §7 and §12.7.
SPEC_ENUMS = {
    enums.ActionStatus: "DRAFT AWAITING_APPROVAL AWAITING_STEPUP READY SUBMITTED SETTLED "
    "REJECTED CANCELLED UNKNOWN PAUSED SUPERSEDED",
    enums.PlanStatus: "draft active paused locked completed partial",
    enums.CheckpointResult: "passed failed checkpoint_missed",
    enums.OperationStatus: "SUBMITTED SETTLED REJECTED UNKNOWN",
    enums.ReceiptStatus: "SETTLED REJECTED",
    enums.CommitmentStatus: "CANDIDATE CONFIRMED PARTIALLY_PAID PAID PAID_REPORTED CANCELLED "
    "DISMISSED",
    enums.BillStatus: "secured unsecured overdue paid paid_reported",
    enums.Tier: "GREEN YELLOW RED FORBIDDEN",
    enums.Provenance: "user_utterance server_record untrusted_text",
    enums.Disposition: "executed clarification infeasible unsupported cancelled no_action",
    enums.ActionKind: "TRANSFER PAY_BILL DESIGNATE_RESERVE CONFIRM_BILL RESOLVE_ITEM CANCEL_BILL "
    "MARK_PAID_EXTERNAL REDEEM PURCHASE CHECK_OUTCOME ADD_PAYEE",
    enums.LockScope: "plan customer global",
    enums.Role: "customer operator demo",
    enums.Channel: "web im",
    enums.PlanKind: "single rebalance mixed",
    enums.PlanMode: "coordinated flat",
    enums.ApprovalScopeKind: "action plan",
    enums.OperationEventKind: "SUBMITTED SETTLED REJECTED UNKNOWN_OBSERVED RECONCILED",
    enums.ReservationState: "ACTIVE CONSUMED RELEASED",
    enums.BankRequestStatus: "EXECUTED REJECTED",
    enums.AuditActor: "customer agent coordinator operator system",
    enums.PayeeBank: "same other",
    enums.Recurrence: "monthly none",
    enums.Fault: "crash_before_commit crash_after_commit pause_before_commit pause_after_commit "
    "drop_response bank_ack_lost settlement_reject probe_timeout busy unexpected tick_crash",
}


@pytest.mark.parametrize("enum", list(SPEC_ENUMS), ids=lambda enum: enum.__name__)
def test_enum_members_equal_the_spec(enum):
    assert [member.value for member in enum] == SPEC_ENUMS[enum].split()


def _expand(sources: str, targets: str) -> set[tuple[str, str]]:
    return {(a, b) for a in sources.split() for b in targets.split()}


# One entry per row of the SPEC §8.1 table; AWAITING_* means both awaiting states.
SPEC_TRANSITIONS = (
    _expand("DRAFT", "AWAITING_APPROVAL")
    | _expand("AWAITING_APPROVAL", "READY")
    | _expand("AWAITING_APPROVAL", "AWAITING_STEPUP")
    | _expand("DRAFT", "AWAITING_STEPUP")
    | _expand("DRAFT", "READY")
    | _expand("DRAFT", "SETTLED")
    | _expand("AWAITING_STEPUP", "READY")
    | _expand("READY", "SUBMITTED SETTLED REJECTED")
    | _expand("READY", "AWAITING_APPROVAL AWAITING_STEPUP")
    | _expand("SUBMITTED", "SETTLED REJECTED")
    | _expand("SUBMITTED READY", "UNKNOWN")
    | _expand("UNKNOWN", "SUBMITTED SETTLED REJECTED")
    | _expand("DRAFT AWAITING_APPROVAL AWAITING_STEPUP READY PAUSED", "CANCELLED")
    | _expand("DRAFT AWAITING_STEPUP READY", "PAUSED")
    | _expand("DRAFT AWAITING_STEPUP READY PAUSED", "SUPERSEDED")
)


def test_action_transitions_equal_the_spec_table():
    pairs = {
        (source.value, target.value)
        for source, targets in ACTION_TRANSITIONS.items()
        for target in targets
    }
    assert pairs == SPEC_TRANSITIONS
    assert set(ACTION_TRANSITIONS) == set(ActionStatus)


def test_terminal_statuses_have_no_exit():
    assert {status.value for status in TERMINAL_ACTION_STATUSES} == {
        "SETTLED",
        "REJECTED",
        "CANCELLED",
        "SUPERSEDED",
    }
    for status in TERMINAL_ACTION_STATUSES:
        assert ACTION_TRANSITIONS[status] == frozenset()
    open_statuses = set(ActionStatus) - TERMINAL_ACTION_STATUSES
    assert all(ACTION_TRANSITIONS[status] for status in open_statuses)


def test_paused_leaves_only_by_cancel_or_new_revision():
    assert ACTION_TRANSITIONS[ActionStatus.PAUSED] == {
        ActionStatus.CANCELLED,
        ActionStatus.SUPERSEDED,
    }


def test_transition_table_is_read_only():
    with pytest.raises(TypeError):
        ACTION_TRANSITIONS[ActionStatus.SETTLED] = frozenset({ActionStatus.DRAFT})  # type: ignore[index]
