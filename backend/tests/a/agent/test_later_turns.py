"""Later turns: the model reads the current state instead of trusting the stored history.

These are the calls the system prompt prescribes for a status question and for a new
e-bill (T61), played by a model that reads the ids it needs from the results.
"""

from __future__ import annotations

from typing import Any

import pytest

from wg.contracts.errors import ErrorCode

from .scripted import Agent, ReadingModel, Results

pytestmark = pytest.mark.layer("U")

EBILL = {
    "customer_id": "cus_main",
    "payee_id": "pay_wangfang",
    "kind": "培训费",
    "amount_fen": 120_000,
    "due_date": "2026-11-11",
}
NEW_BILL = "我刚收到一张1200元的培训费新账单，请帮我确认。"
Calls = list[tuple[str, dict[str, Any]]]


def read_bills(_: Results) -> Calls:
    return [("get_bills", {"intent": "plan.manage"})]


def candidate_id(results: Results) -> str:
    [bills] = [r["bills"] for r in results if "bills" in r]
    [candidate] = [b["commitment_id"] for b in bills if b["status"] == "CANDIDATE"]
    return candidate


def confirm_candidate(results: Results) -> Calls:
    return [("propose_bill", {"intent": "bills.manage", "commitment_id": candidate_id(results)})]


def show_card(results: Results) -> Calls:
    [draft] = [r for r in results if r.get("kind") == "CONFIRM_BILL"]
    lid = draft["logical_action_id"]
    return [("request_approval", {"intent": "bills.manage", "logical_action_id": lid})]


def pay_candidate(results: Results) -> Calls:
    return [("pay_bill", {"intent": "bills.manage", "commitment_id": candidate_id(results)})]


def received(agent: Agent) -> None:
    assert agent.browser.sim("/sim/bills/ebill", EBILL).status_code in (200, 201)


def test_a_new_ebill_is_confirmed_from_its_candidate_record(agent: Agent):
    received(agent)
    done = agent.play(NEW_BILL, ReadingModel([read_bills, confirm_candidate, show_card]))
    assert all(o.ok for o in done.turn.outcomes), [o.error for o in done.turn.outcomes]
    assert len(done.reply.facts.facts) > 0
    [card] = agent.rows(
        "SELECT c.kind, c.approval_id, a.kind AS action FROM cards c"
        " JOIN actions a ON a.logical_action_id = c.logical_action_id"
    )
    assert card == {"kind": "action", "approval_id": None, "action": "CONFIRM_BILL"}
    # Only the customer's confirmation on the card makes it a bill.
    assert agent.rows("SELECT status FROM commitments WHERE kind = '培训费'") == [
        {"status": "CANDIDATE"}
    ]


def test_a_candidate_bill_still_cannot_be_paid_before_it_is_confirmed(agent: Agent):
    received(agent)
    done = agent.play(NEW_BILL, ReadingModel([read_bills, pay_candidate]))
    [_, paid] = done.turn.outcomes
    assert paid.error is not None
    assert agent.rows("SELECT COUNT(*) AS n FROM cards") == [{"n": 0}]
    assert agent.rows("SELECT COUNT(*) AS n FROM actions WHERE kind = 'PAY_BILL'") == [{"n": 0}]


def test_plan_status_is_read_under_the_plan_intent_and_denied_under_the_bills_intent(
    agent: Agent,
):
    def under(intent: str):
        return lambda _: [("get_plan_status", {"intent": intent})]

    done = agent.play("进度怎么样了？", ReadingModel([under("bills.manage"), under("plan.manage")]))
    denied, read = done.turn.outcomes
    assert denied.error is not None and denied.error.code is ErrorCode.READ_SCOPE_DENIED
    assert read.ok
    assert agent.rows("SELECT event FROM audit_log WHERE event = 'extra_read'") == [
        {"event": "extra_read"}
    ]
