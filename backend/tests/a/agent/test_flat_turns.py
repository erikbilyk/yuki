"""The combined request in flat mode (SPEC §26): no plan, each draft on its own card.

The model plays the three steps of the system prompt; `compose_plan` answers `flat_mode`, and
the model then asks for each draft's own card as the flat-mode lines of the prompt say.
Nothing runs until the customer confirms a card and enters the code.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import pytest

from wg.agent.loop import MAX_TOOL_CALLS
from wg.agent.prompt import load_system_prompt
from wg.contracts.enums import PlanMode
from wg.contracts.errors import ErrorCode
from wg.k6.simtime import SHANGHAI

from .scripted import Agent, ReadingModel, Results
from .test_combined_request import MESSAGE, compose_step, first_step, second_step

pytestmark = pytest.mark.layer("U")

EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)
RENT_DUE = "今天房租到期了，请帮我交掉。"
Calls = list[tuple[str, dict[str, Any]]]


def flat_lines() -> list[str]:
    return [line for line in load_system_prompt().text.splitlines() if "flat" in line]


def approval(logical_action_id: str, intent: str) -> tuple[str, dict[str, Any]]:
    return ("request_approval", {"intent": intent, "logical_action_id": logical_action_id})


def reserve_and_redemption(results: Results) -> Calls:
    [reserve] = [r for r in results if r.get("kind") == "DESIGNATE_RESERVE"]
    [plan] = [r for r in results if "plan_id" in r]
    [redeem] = [a for a in plan["actions"] if a["kind"] == "REDEEM"]
    return [
        approval(reserve["logical_action_id"], "cash.analyze"),
        approval(redeem["logical_action_id"], "funds.rebalance"),
    ]


def read_state(_: Results) -> Calls:
    return [
        ("get_plan_status", {"intent": "plan.manage"}),
        ("get_bills", {"intent": "plan.manage"}),
    ]


def flat_nodes(results: Results) -> dict[str, dict[str, Any]]:
    [status] = [r for r in results if "plans" in r]
    [plan] = [p for p in status["plans"] if p["mode"] == "flat"]
    return {node["kind"]: node for node in plan["nodes"]}


def redemption_purchase_and_rent(results: Results) -> Calls:
    nodes = flat_nodes(results)
    return [
        approval(nodes["REDEEM"]["logical_action_id"], "funds.rebalance"),
        approval(nodes["PURCHASE"]["logical_action_id"], "funds.rebalance"),
        ("pay_bill", {"intent": "bills.manage", "commitment_id": "cmt_rent"}),
    ]


def rent_card(results: Results) -> Calls:
    [rent] = [r for r in results if r.get("kind") == "PAY_BILL" and r.get("status") == "DRAFT"]
    return [approval(rent["logical_action_id"], "bills.manage")]


def action_cards(agent: Agent) -> list[dict[str, Any]]:
    return agent.rows(
        "SELECT c.kind, c.approval_id, a.kind AS action, a.status FROM cards c"
        " JOIN actions a ON a.logical_action_id = c.logical_action_id ORDER BY a.kind"
    )


def nothing_ran(agent: Agent) -> None:
    assert agent.rows("SELECT COUNT(*) AS n FROM approvals") == [{"n": 0}]
    assert agent.rows("SELECT COUNT(*) AS n FROM challenges") == [{"n": 0}]
    # The fixture's opening balance is the only operation without an action.
    ran = "SELECT COUNT(*) AS n FROM operations WHERE logical_action_id IS NOT NULL"
    assert agent.rows(ran) == [{"n": 0}]
    assert agent.rows("SELECT COUNT(*) AS n FROM plans WHERE kind = 'mixed'") == [{"n": 0}]
    assert agent.rows("SELECT status FROM commitments WHERE commitment_id = 'cmt_rent'") == [
        {"status": "CONFIRMED"}
    ]


def test_a_flat_turn_asks_for_each_draft_card_after_compose_is_refused(agent: Agent):
    model = ReadingModel([first_step, second_step, compose_step(), reserve_and_redemption])
    done = agent.play(MESSAGE, model, mode=PlanMode.FLAT)
    [composed] = [o for o in done.turn.outcomes if o.name == "compose_plan"]
    assert composed.error is not None and composed.error.code is ErrorCode.UNSUPPORTED_REQUEST
    assert composed.error.detail == {"reason": "flat_mode"}
    # Seven calls for the three steps leave one for the cards: the second waits a turn.
    shown, over = [o for o in done.turn.outcomes if o.name == "request_approval"]
    assert shown.ok and over.error is not None
    assert over.error.detail == {"reason": "tool_call_limit"}
    assert done.turn.limit_reached and len(done.turn.outcomes) == MAX_TOOL_CALLS + 1
    assert action_cards(agent) == [
        {
            "kind": "action",
            "approval_id": None,
            "action": "DESIGNATE_RESERVE",
            "status": "AWAITING_APPROVAL",
        }
    ]
    nothing_ran(agent)


def test_a_later_flat_turn_opens_the_remaining_cards_and_still_runs_nothing(agent: Agent):
    agent.play(
        MESSAGE,
        ReadingModel([first_step, second_step, compose_step(), reserve_and_redemption]),
        mode=PlanMode.FLAT,
    )
    target = (EPOCH + timedelta(days=2)).isoformat()
    assert agent.browser.sim("/sim/clock/advance", {"to": target}).status_code == 200
    model = ReadingModel([read_state, redemption_purchase_and_rent, rent_card])
    done = agent.play(RENT_DUE, model, mode=PlanMode.FLAT)
    assert len(done.turn.outcomes) == 6 and not done.turn.limit_reached
    by_name = {}
    for outcome in done.turn.outcomes:
        by_name.setdefault(outcome.name, []).append(outcome)
    redemption, purchase, rent = by_name["request_approval"]
    assert redemption.ok and rent.ok
    # The gateway still holds the purchase until the redemption has settled.
    assert purchase.error is not None
    assert ErrorCode.UNSETTLED_FUNDS.value in purchase.error.detail["reasons"]
    cards = action_cards(agent)
    assert [card["action"] for card in cards] == ["DESIGNATE_RESERVE", "PAY_BILL", "REDEEM"]
    assert {card["kind"] for card in cards} == {"action"}
    assert {card["status"] for card in cards} == {"AWAITING_APPROVAL"}
    nothing_ran(agent)


def test_the_flat_lines_apply_only_after_a_flat_refusal_and_keep_the_three_steps():
    text = load_system_prompt().text
    steps = re.findall(r"^\s+\d\. (.+)$", text, flags=re.MULTILINE)
    assert len(steps) == 3 and not any("flat" in step for step in steps)
    assert "客户在方案卡片上统一确认，不必再逐笔 request_approval。" in text
    first, later = flat_lines()
    assert first.startswith("- 如果 compose_plan 返回 UNSUPPORTED_REQUEST，reason 为 flat_mode")
    assert later.startswith("- get_plan_status 里 mode 为 flat 的方案")
    # The only way forward the flat lines name is the customer's own card.
    named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", first + later))
    assert named == {
        "compose_plan",
        "flat_mode",
        "get_plan_status",
        "logical_action_id",
        "request_approval",
    }
