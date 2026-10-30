"""How a chat turn's tool outcomes become the reply's plan and disposition."""

import pytest

from wg.agent.chat import disposition, newest_plan_id
from wg.agent.registry import ToolOutcome
from wg.contracts.api import ApprovalRequestResult, PlanDraftResult
from wg.contracts.enums import Disposition
from wg.contracts.errors import ErrorCode
from wg.contracts.tools import ToolError

pytestmark = pytest.mark.layer("U")


def plan(plan_id: str) -> PlanDraftResult:
    return PlanDraftResult.model_construct(plan_id=plan_id, actions=())


def refused(name: str, code: ErrorCode) -> ToolOutcome:
    return ToolOutcome(name, None, error=ToolError(code=code, detail={}))


def test_the_reply_names_the_plan_composed_after_the_draft():
    assert newest_plan_id([plan("pln_draft"), plan("pln_mixed")]) == "pln_mixed"
    assert newest_plan_id([]) is None


def test_a_refused_side_call_does_not_make_a_planned_turn_unsupported():
    outcomes = [
        ToolOutcome("draft_rebalance_plan", None, result=plan("pln_draft")),
        refused("request_approval", ErrorCode.UNSUPPORTED_REQUEST),
    ]
    assert disposition(outcomes) is None


def test_a_refused_side_call_does_not_make_a_carded_turn_unsupported():
    card = ApprovalRequestResult.model_construct(card_id="crd_1", logical_action_id="act_1")
    outcomes = [
        refused("compose_plan", ErrorCode.UNSUPPORTED_REQUEST),
        ToolOutcome("request_approval", None, result=card),
    ]
    assert disposition(outcomes) is None


def test_a_turn_that_prepared_nothing_stays_unsupported():
    assert disposition([refused("compose_plan", ErrorCode.UNSUPPORTED_REQUEST)]) is (
        Disposition.UNSUPPORTED
    )
    assert disposition([refused("x", ErrorCode.FORBIDDEN_ROUTE)]) is Disposition.UNSUPPORTED
