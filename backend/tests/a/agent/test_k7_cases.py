"""Scripted-model turns for K7: a reply that claims more than the records say (T15)."""

from __future__ import annotations

from typing import Any

import pytest

from wg.agent.turn import AgentTurn
from wg.contracts.facts import FactConcept
from wg.core.k7_verify import REPLACED_EVENT

from .scripted import Agent, only_outcome

pytestmark = pytest.mark.layer("U")


def submitted_to_dad(agent: Agent) -> str:
    """A ¥500 transfer to the other-bank payee, confirmed and still SUBMITTED."""
    arguments = {
        "intent": "transfer.create",
        "payee_ref": "pay_dad",
        "payee_quote": "爸爸",
        "amount_fen": 50_000,
        "amount_quote": "500",
    }
    turn = agent.turn(
        "给爸爸转500", [{"tool_calls": [tool("draft_transfer", arguments)]}, text("")]
    )
    lid = only_outcome(turn).result.logical_action_id
    assert agent.confirm(lid)["action_status"] == "SUBMITTED"
    return lid


def tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def text(body: str) -> dict[str, Any]:
    return {"text": body}


def status_turn(agent: Agent, lid: str, explanation: str) -> AgentTurn:
    status = tool("get_operation_status", {"intent": "transfer.history", "logical_action_id": lid})
    return agent.turn("给爸爸的钱到了吗？", [{"tool_calls": [status]}, text(explanation)])


def replacements(agent: Agent) -> int:
    return len(agent.rows("SELECT event FROM audit_log WHERE event = ?", REPLACED_EVENT))


@pytest.mark.case("T15")
def test_t15_transferred_said_of_a_submitted_transfer_is_replaced(agent: Agent):
    lid = submitted_to_dad(agent)
    result = status_turn(agent, lid, "已转账给爸爸500元。")
    reply = result.reply
    assert reply.replaced and reply.explanation != "已转账给爸爸500元。"
    assert "已转账" not in (reply.explanation or "")
    assert any("已提交，银行处理中" in line for line in reply.fact_lines)
    assert replacements(agent) == 1


@pytest.mark.case("T15")
def test_t15_a_wrong_amount_is_replaced(agent: Agent):
    lid = submitted_to_dad(agent)
    reply = status_turn(agent, lid, "给爸爸的5000元已提交，银行处理中。").reply
    assert reply.replaced
    assert [v.reason.value for v in reply.violations] == ["unsupported"]


@pytest.mark.case("T15")
def test_t15_the_true_status_and_amount_are_kept(agent: Agent):
    lid = submitted_to_dad(agent)
    explanation = "给爸爸的500元已提交，银行正在处理，到账后会生成回单。"
    reply = status_turn(agent, lid, explanation).reply
    assert not reply.replaced and reply.explanation == explanation
    assert replacements(agent) == 0


@pytest.mark.case("T15")
def test_t15_transferred_is_kept_once_the_receipt_says_settled(agent: Agent):
    lid = submitted_to_dad(agent)
    assert agent.browser.sim("/sim/clock/advance", {"days": 1}).status_code == 200
    reply = status_turn(agent, lid, "已转账给爸爸500元，已到账。").reply
    assert not reply.replaced


@pytest.mark.case("T15")
def test_t15_a_draft_reply_that_claims_payment_falls_back_to_the_card_template(agent: Agent):
    script = [
        {
            "tool_calls": [
                tool(
                    "draft_transfer",
                    {
                        "intent": "transfer.create",
                        "payee_ref": "pay_mom",
                        "payee_quote": "妈妈",
                        "amount_fen": 50_000,
                        "amount_quote": "500",
                    },
                )
            ]
        },
        text("已支付500元给妈妈。"),
    ]
    reply = agent.turn("给妈妈转500", script).reply
    assert reply.replaced and reply.explanation is not None
    assert "草稿" in reply.explanation


def snapshot_turn(agent: Agent, explanation: str) -> AgentTurn:
    snapshot = tool("get_cash_snapshot", {"intent": "cash.analyze"})
    return agent.turn("还有多少可以自由支配？", [{"tool_calls": [snapshot]}, text(explanation)])


@pytest.mark.parametrize(
    ("explanation", "replaced"),
    [
        ("您目前可自由支配的金额是¥2,000。", False),
        ("您目前可自由支配的金额是¥16,000。", True),
        ("保险已保障，尚未支付，房租也已保障。", False),
        ("保险已支付。", True),
        ("您账户现金合计18,000元，其中受保护资金16,000元，可自由支配2,000元。", False),
        ("您可自由支配的金额是18,000元。", True),
    ],
)
def test_snapshot_claims_must_match_the_fact_of_the_same_meaning(
    agent: Agent, explanation: str, replaced: bool
):
    reply = snapshot_turn(agent, explanation).reply
    assert reply.replaced is replaced
    assert (reply.explanation == explanation) is not replaced


@pytest.mark.case("T17")
def test_t17_the_posted_cash_total_is_a_fact_of_the_snapshot(agent: Agent):
    reply = snapshot_turn(agent, "现金合计18,000元，受保护16,000元，可自由支配2,000元。").reply
    posted = [f for f in reply.facts.facts if f.concept is FactConcept.POSTED_CASH]
    assert [(f.value, f.label) for f in posted] == [(1_800_000, "账户现金合计")]
    assert "账户现金合计：¥18,000.00" in reply.fact_lines
    assert not reply.replaced
