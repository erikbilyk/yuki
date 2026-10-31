"""A combined request within the 8-call budget: the three steps the system prompt prescribes.

The model is a `ReadingModel`: it reads the ids it needs from the tool results it was sent,
as a real model does. The T60 message asks for a reserve, a rebalance and two bills.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from wg.agent.loop import MAX_TOOL_CALLS
from wg.agent.prompt import load_system_prompt
from wg.contracts.errors import ErrorCode
from wg.contracts.tools import TOOLS_BY_NAME

from .scripted import Agent, ReadingModel, Results, Step

pytestmark = pytest.mark.layer("U")

MESSAGE = (
    "请帮我做这几件事：应急备用金提到5000元；科技类基金的占比在11月7日前降到40%以下，"
    "手续费不超过20元；房租11月4日到期、保险11月9日到期，到期日再交。先给我看方案，我确认后再动钱。"
)
SIX_NODES = ["CHECK_OUTCOME", "DESIGNATE_RESERVE", "PAY_BILL", "PAY_BILL", "PURCHASE", "REDEEM"]
EXTRA_READS = [
    ("get_cash_snapshot", {"intent": "cash.analyze"}),
    ("get_accounts", {"intent": "balance.query"}),
    ("get_positions", {"intent": "funds.rebalance"}),
    ("get_portfolio_exposure", {"intent": "funds.rebalance", "key": "technology"}),
]


def first_step(_: Results) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "propose_reserve_change",
            {
                "intent": "cash.analyze",
                "target_fen": 500_000,
                "amount_quote": "应急备用金提到5000元",
            },
        ),
        (
            "search_rebalance_candidates",
            {
                "intent": "funds.rebalance",
                "sector": "technology",
                "target_max_pct": "40",
                "target_quote": "降到40%以下",
                "fee_cap_fen": 2_000,
                "fee_cap_quote": "手续费不超过20元",
                "checkpoint_day": 5,
            },
        ),
        ("get_bills", {"intent": "bills.manage"}),
    ]


def second_step(results: Results) -> list[tuple[str, dict[str, Any]]]:
    [search] = [r for r in results if "calc_result_id" in r and "selected" in r]
    [bills] = [r["bills"] for r in results if "bills" in r]
    return [
        (
            "draft_rebalance_plan",
            {
                "intent": "funds.rebalance",
                "calc_result_id": search["calc_result_id"],
                "with_bounds": True,
            },
        ),
        *(
            ("pay_bill", {"intent": "bills.manage", "commitment_id": bill["commitment_id"]})
            for bill in bills
            if bill["status"] == "CONFIRMED"
        ),
    ]


def compose_step(*, purchase_waits_for_redemption: bool = True) -> Step:
    def step(results: Results) -> list[tuple[str, dict[str, Any]]]:
        drafts = [r for r in results if r.get("kind") in ("DESIGNATE_RESERVE", "PAY_BILL")]
        [plan] = [r for r in results if "plan_id" in r]
        nodes = {a["kind"]: a["logical_action_id"] for a in plan["actions"]}
        depends_on = {}
        if purchase_waits_for_redemption:
            depends_on[nodes["PURCHASE"]] = [nodes["REDEEM"]]
        draft_ids = [d["logical_action_id"] for d in drafts] + list(nodes.values())
        return [
            (
                "compose_plan",
                {"intent": "plan.manage", "draft_ids": draft_ids, "depends_on": depends_on},
            )
        ]

    return step


def plan_nodes(agent: Agent) -> list[str]:
    return [
        row["kind"]
        for row in agent.rows(
            "SELECT a.kind FROM actions a JOIN plans p ON p.plan_id = a.plan_id"
            " WHERE p.kind = 'mixed' ORDER BY a.kind"
        )
    ]


def test_the_prescribed_three_steps_compose_all_six_nodes_within_the_budget(agent: Agent):
    model = ReadingModel([first_step, second_step, compose_step()])
    done = agent.play(MESSAGE, model)
    assert len(done.turn.outcomes) == 7 < MAX_TOOL_CALLS and not done.turn.limit_reached
    assert all(outcome.ok for outcome in done.turn.outcomes), [o.error for o in done.turn.outcomes]
    assert plan_nodes(agent) == SIX_NODES
    [card] = agent.rows(
        "SELECT c.kind, c.approval_id FROM cards c JOIN plans p ON p.plan_id = c.plan_id"
        " WHERE p.kind = 'mixed'"
    )
    assert card == {"kind": "plan", "approval_id": None}
    plans = agent.registry.services.plans
    [summary] = plans.plans("cus_main").plans
    assert plans.plan("cus_main", summary.plan_id).bounds is not None
    assert done.reply.explanation is not None


def test_extra_reads_before_the_drafts_leave_compose_over_the_budget(agent: Agent):
    reads: Step = lambda _: list(EXTRA_READS)  # noqa: E731
    model = ReadingModel([reads, first_step, second_step, compose_step()])
    done = agent.play(MESSAGE, model)
    assert done.turn.limit_reached
    composed = [o for o in done.turn.outcomes if o.name == "compose_plan"]
    assert all(o.error is not None for o in composed)
    assert all(o.error.detail == {"reason": "tool_call_limit"} for o in composed if o.error)
    assert plan_nodes(agent) == []
    assert agent.rows("SELECT COUNT(*) AS n FROM approvals") == [{"n": 0}]


def test_a_composed_plan_without_the_purchase_dependency_is_still_refused(agent: Agent):
    model = ReadingModel(
        [first_step, second_step, compose_step(purchase_waits_for_redemption=False)]
    )
    done = agent.play(MESSAGE, model)
    [composed] = [o for o in done.turn.outcomes if o.name == "compose_plan"]
    assert composed.error is not None
    assert ErrorCode.UNFUNDED_DEPENDENCY.value in json.dumps(composed.error.detail)
    assert plan_nodes(agent) == []


def identifiers(text: str) -> set[str]:
    return set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", text))


def test_every_name_in_the_prompt_is_a_registered_tool_one_of_its_fields_or_a_reason():
    fields = {name for tool in TOOLS_BY_NAME.values() for name in tool.arguments.model_fields}
    fields |= {"calc_result_id", "commitment_id", "logical_action_id"}
    # The refusal reason compose_plan returns in flat mode (asserted in test_flat_turns).
    fields |= {"flat_mode"}
    # Result fields of the direct-debit tools that the prompt tells the model how to read.
    fields |= {"description_raw", "effect_check"}
    named = identifiers(load_system_prompt().text)
    assert named & set(TOOLS_BY_NAME), "the prompt names no tool"
    assert named <= set(TOOLS_BY_NAME) | fields, named - set(TOOLS_BY_NAME) - fields


def test_the_combined_request_steps_fit_the_budget_with_two_bills():
    text = load_system_prompt().text
    steps = re.findall(r"^\s+\d\. (.+)$", text, flags=re.MULTILINE)
    assert len(steps) == 3
    tools = [identifiers(step) & set(TOOLS_BY_NAME) for step in steps]
    # Step 2 drafts one pay_bill per bill; two bills make it three calls.
    calls = len(tools[0]) + (len(tools[1]) + 1) + len(tools[2])
    assert calls == 7 < MAX_TOOL_CALLS


def test_the_rebalance_step_asks_for_bounds_the_customer_approves_on_the_card():
    steps = re.findall(r"^\s+\d\. (.+)$", load_system_prompt().text, flags=re.MULTILINE)
    assert "draft_rebalance_plan" in steps[1] and "with_bounds 设为 true" in steps[1]
