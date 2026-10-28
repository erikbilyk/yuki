import pytest
from pydantic import ValidationError

from wg.contracts.events import EVENT_PAYLOADS, CommitmentsChanged, DomainEventType
from wg.contracts.stub_script import StubScript, StubTextTurn, StubToolCallsTurn

SPEC_EVENTS = (
    "commitments_changed operation_submitted operation_settled operation_rejected "
    "operation_unknown plan_revision_created checkpoint_recorded lock_set lock_cleared "
    "clock_advanced fault_fired"
)


def test_every_event_type_has_a_payload_model():
    assert [event.value for event in DomainEventType] == SPEC_EVENTS.split()
    assert set(EVENT_PAYLOADS) == set(DomainEventType)


def test_commitments_changed_payload():
    payload = CommitmentsChanged(
        commitment_event_id=7,
        commitment_id="cmt_new",
        purpose_id="pur_new",
        change="confirmed",
        delta_fen=120000,
        unpaid_fen=120000,
        commitments_version=3,
    )
    assert payload.model_dump(mode="json")["change"] == "confirmed"
    with pytest.raises(ValidationError):
        CommitmentsChanged.model_validate(payload.model_dump() | {"delta_fen": 1200.0})


def test_stub_script_accepts_tool_calls_and_text():
    script = StubScript.model_validate(
        [
            {"tool_calls": [{"name": "get_cash_snapshot", "arguments": {}}]},
            {"tool_calls": [{"name": "draft_transfer", "arguments": {"amount_fen": 50000}}]},
            {"text": "已为您准备好转账卡片。"},
        ]
    )
    kinds = [type(turn) for turn in script.root]
    assert kinds == [StubToolCallsTurn, StubToolCallsTurn, StubTextTurn]


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"tool_calls": []},
        {"tool_calls": [{"arguments": {}}]},
        {"text": "x", "tool_calls": [{"name": "a"}]},
        {"reply": "x"},
    ],
)
def test_stub_script_refuses_malformed_turns(item):
    with pytest.raises(ValidationError):
        StubScript.model_validate([item])
