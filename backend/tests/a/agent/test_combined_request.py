"""A combined request within the 8-call budget: the three steps the system prompt prescribes.

The model is played by a provider that reads the ids it needs from the tool results it was
sent, as a real model does. The T60 message asks for a reserve, a rebalance and two bills.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from wg.agent.llm import ModelAdapter, ModelReply, ToolCallRequest
from wg.agent.loop import MAX_TOOL_CALLS
from wg.agent.prompt import load_system_prompt
from wg.agent.turn import AgentTurn, agent_turn
from wg.contracts.enums import Channel
from wg.contracts.errors import ErrorCode
from wg.contracts.tools import TOOLS_BY_NAME

from ..api.conftest import make_settings
from .scripted import Agent

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

Results = list[dict[str, Any]]
Step = Callable[[Results], list[tuple[str, dict[str, Any]]]]


class ReadingModel:
    """Plays one model reply per step; each step sees the tool results sent since the last."""

    name = "reading"

    def __init__(self, steps: Sequence[Step], final_text: str = "已为您准备好，等待您确认。"):
        self.steps = list(steps)
        self.final_text = final_text
        self.ids = count()
        self.results: Results = []

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelReply:
        latest = []
        for message in reversed(messages):
            if message["role"] != "tool":
                break
            latest.insert(0, json.loads(message["content"]))
        self.results.extend(latest)
        if not self.steps or not tools:
            return ModelReply(text=self.final_text, returned_model="test")
        calls = self.steps.pop(0)(self.results)
        return ModelReply(
            text="",
            tool_calls=tuple(
                ToolCallRequest(f"call_{next(self.ids)}", name, json.dumps(args), args)
                for name, args in calls
            ),
            returned_model="test",
        )


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


def play(agent: Agent, tmp_path: Path, model: ReadingModel) -> AgentTurn:
    adapter = ModelAdapter(
        model, make_settings(tmp_path / "wg.db"), log_path=tmp_path / "llm_calls.jsonl"
    )
    return agent_turn(
        adapter, agent.registry, customer_id="cus_main", channel=Channel.WEB, text=MESSAGE
    )


def plan_nodes(agent: Agent) -> list[str]:
    return [
        row["kind"]
        for row in agent.rows(
            "SELECT a.kind FROM actions a JOIN plans p ON p.plan_id = a.plan_id"
            " WHERE p.kind = 'mixed' ORDER BY a.kind"
        )
    ]


def test_the_prescribed_three_steps_compose_all_six_nodes_within_the_budget(
    agent: Agent, tmp_path: Path
):
    model = ReadingModel([first_step, second_step, compose_step()])
    done = play(agent, tmp_path, model)
    assert len(done.turn.outcomes) == 7 < MAX_TOOL_CALLS and not done.turn.limit_reached
    assert all(outcome.ok for outcome in done.turn.outcomes), [o.error for o in done.turn.outcomes]
    assert plan_nodes(agent) == SIX_NODES
    [card] = agent.rows(
        "SELECT c.kind, c.approval_id FROM cards c JOIN plans p ON p.plan_id = c.plan_id"
        " WHERE p.kind = 'mixed'"
    )
    assert card == {"kind": "plan", "approval_id": None}
    assert done.reply.explanation is not None


def test_extra_reads_before_the_drafts_leave_compose_over_the_budget(agent: Agent, tmp_path: Path):
    reads: Step = lambda _: list(EXTRA_READS)  # noqa: E731
    model = ReadingModel([reads, first_step, second_step, compose_step()])
    done = play(agent, tmp_path, model)
    assert done.turn.limit_reached
    composed = [o for o in done.turn.outcomes if o.name == "compose_plan"]
    assert all(o.error is not None for o in composed)
    assert all(o.error.detail == {"reason": "tool_call_limit"} for o in composed if o.error)
    assert plan_nodes(agent) == []
    assert agent.rows("SELECT COUNT(*) AS n FROM approvals") == [{"n": 0}]


def test_a_composed_plan_without_the_purchase_dependency_is_still_refused(
    agent: Agent, tmp_path: Path
):
    model = ReadingModel(
        [first_step, second_step, compose_step(purchase_waits_for_redemption=False)]
    )
    done = play(agent, tmp_path, model)
    [composed] = [o for o in done.turn.outcomes if o.name == "compose_plan"]
    assert composed.error is not None
    assert ErrorCode.UNFUNDED_DEPENDENCY.value in json.dumps(composed.error.detail)
    assert plan_nodes(agent) == []


def identifiers(text: str) -> set[str]:
    return set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", text))


def test_every_name_in_the_prompt_is_a_registered_tool_or_one_of_its_fields():
    fields = {name for tool in TOOLS_BY_NAME.values() for name in tool.arguments.model_fields}
    fields |= {"calc_result_id", "commitment_id", "logical_action_id"}
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
