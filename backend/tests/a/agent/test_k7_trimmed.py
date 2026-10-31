"""K7 and clarifying questions: unsupported asides are cut, the question is kept."""

from __future__ import annotations

from typing import Any

import pytest

from wg.agent.turn import AgentTurn
from wg.core.k7_verify import (
    REPLACED_EVENT,
    TEMPLATE_DRAFT,
    TEMPLATE_EMPTY,
    TEMPLATE_FACTS,
)

from .scripted import Agent, draft

pytestmark = pytest.mark.layer("U")

SNAPSHOT = {"name": "get_cash_snapshot", "arguments": {"intent": "cash.analyze"}}


def reply_to(agent: Agent, text: str, explanation: str, *calls: dict[str, Any]) -> AgentTurn:
    script = [{"tool_calls": list(calls)}] if calls else []
    return agent.turn(text, [*script, {"text": explanation}])


def decisions(agent: Agent) -> list[tuple[str, str]]:
    rows = agent.rows("SELECT decision, reason FROM audit_log WHERE event = ?", REPLACED_EVENT)
    return [(r["decision"], r["reason"]) for r in rows]


def test_example_amounts_are_cut_from_a_missing_amount_question(agent: Agent):
    reply = reply_to(agent, "给妈妈转点钱", "请问您要给妈妈转多少钱？比如500元或1000元。").reply
    assert reply.replaced and reply.explanation == "请问您要给妈妈转多少钱？"
    assert [v.text for v in reply.violations] == ["500元", "1000元"]
    assert decisions(agent) == [("trimmed,unsupported", "2")]


def test_invented_holdings_are_cut_from_a_which_fund_question(agent: Agent):
    explanation = "您持有两只基金，市值分别约12,000元和8,000元。请问您要卖出哪一只？"
    reply = reply_to(agent, "把那只基金卖了", explanation).reply
    assert reply.replaced and reply.explanation == "请问您要卖出哪一只？"
    assert decisions(agent) == [("trimmed,unsupported", "2")]


def test_the_question_keeps_its_supported_sentences(agent: Agent):
    explanation = "您目前可自由支配¥2,000。上月房租大约3,500元。请问这次要转多少？"
    reply = reply_to(agent, "我想给妈妈转点钱", explanation, SNAPSHOT).reply
    assert reply.replaced
    assert reply.explanation == "您目前可自由支配¥2,000。请问这次要转多少？"


def test_without_a_question_left_the_reply_is_replaced(agent: Agent):
    reply = reply_to(agent, "给妈妈转点钱", "常见的金额是500元。我先帮您查一下。").reply
    assert reply.replaced and reply.explanation == TEMPLATE_EMPTY
    assert decisions(agent) == [("unsupported", "1")]


def test_a_mismatch_replaces_the_whole_question(agent: Agent):
    reply = reply_to(agent, "能转多少", "您可自由支配20,000元。请问要转多少？", SNAPSHOT).reply
    assert reply.replaced and reply.explanation == TEMPLATE_FACTS
    assert decisions(agent) == [("value_mismatch", "1")]


def test_a_status_claim_replaces_the_whole_question(agent: Agent):
    reply = reply_to(agent, "保险怎么样了", "保险已支付，金额800元。请问还要做什么？").reply
    assert reply.replaced and reply.explanation == TEMPLATE_EMPTY
    assert decisions(agent) == [("status_unsupported,unsupported", "2")]


def test_a_turn_with_a_draft_is_not_trimmed(agent: Agent):
    explanation = "草稿已生成，另外建议再转5000元。请问需要现在确认吗？"
    reply = agent.turn("给妈妈转500", [draft(50_000, "500"), {"text": explanation}]).reply
    assert reply.replaced and reply.explanation == TEMPLATE_DRAFT
    assert decisions(agent) == [("unsupported", "1")]


def test_the_rest_must_pass_the_claim_check_as_it_is(agent: Agent):
    explanation = "您账上大约还有12,000元。请问这500元转给王磊还是王芳？"
    reply = reply_to(agent, "给小王转500", explanation).reply
    assert reply.replaced and reply.explanation == TEMPLATE_EMPTY
    assert [v.text for v in reply.violations] == ["12,000元"]
    assert decisions(agent) == [("unsupported", "1")]
