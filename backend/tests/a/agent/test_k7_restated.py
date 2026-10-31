"""K7 and values the customer wrote: a reply may restate them, never pass them off as records."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from wg.agent.turn import AgentTurn
from wg.contracts.facts import (
    ClaimViolationReason,
    Fact,
    FactBlock,
    FactConcept,
    FactKind,
    MoneyFact,
    PercentFact,
    StatusFact,
)
from wg.core.k7_verify import REPLACED_EVENT, customer_values, remaining_violations
from wg.textcheck.claims import FEASIBLE_WITH_APPROVAL

from .scripted import Agent

pytestmark = pytest.mark.layer("U")

AT = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
T35_REQUEST = "把消费电子的占比降到40%以下，手续费不超过20元，11月7日前完成"


def tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def reply_to(
    agent: Agent,
    text: str,
    explanation: str,
    calls: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> AgentTurn:
    script = [{"tool_calls": calls}] if calls else []
    return agent.turn(text, [*script, {"text": explanation}], history=history or [])


def replacements(agent: Agent) -> list[dict[str, Any]]:
    return agent.rows("SELECT decision, reason FROM audit_log WHERE event = ?", REPLACED_EVENT)


def money(fen: int, concept: FactConcept, label: str, n: int = 1) -> MoneyFact:
    return MoneyFact(
        fact_id=f"f{n}", concept=concept, source_ref="calc", as_of=AT, label=label, value=fen
    )


def percent(value: str, concept: FactConcept, label: str, n: int = 1) -> PercentFact:
    return PercentFact(
        fact_id=f"f{n}",
        concept=concept,
        source_ref="calc",
        as_of=AT,
        label=label,
        value=Decimal(value),
    )


def left(text: str, facts: list[Fact], *customer: str) -> list[tuple[str, str]]:
    block = FactBlock(facts=tuple(facts))
    return [(v.text, v.reason.value) for v in remaining_violations(text, block, customer)]


FEE_15 = money(1_500, FactConcept.FEE, "推荐方案手续费")
SHARE_45 = percent("45.1200", FactConcept.SECTOR_SHARE, "消费电子占比（上限）", n=2)


# Restated values that are kept


@pytest.mark.case("T3")
def test_t3_a_question_that_repeats_the_amount_is_kept(agent: Agent):
    question = "您有两位姓王的收款人：王磊和王芳。请问这500元要转给哪一位？"
    reply = reply_to(agent, "给小王转500", question).reply
    assert not reply.replaced and reply.explanation == question
    assert replacements(agent) == []


@pytest.mark.case("T3")
def test_t3_the_amount_of_an_earlier_customer_message_counts(agent: Agent):
    history = [
        {"role": "user", "content": "给小王转500"},
        {"role": "assistant", "content": "请问是王磊还是王芳？"},
    ]
    question = "好的，给王芳转500元，确认要现在生成草稿吗？"
    reply = reply_to(agent, "王芳", question, history=history).reply
    assert not reply.replaced and reply.explanation == question


@pytest.mark.case("T4")
def test_t4_a_colloquial_amount_restated_is_kept(agent: Agent):
    question = "您说的老妈是收款人“妈妈”吗？确认后我为您准备给妈妈转500元的草稿。"
    reply = reply_to(agent, "给老妈打500块", question).reply
    assert not reply.replaced and reply.explanation == question


@pytest.mark.case("T35")
def test_t35_the_customer_limits_restated_next_to_snapshot_facts_are_kept(agent: Agent):
    snapshot = tool("get_cash_snapshot", {"intent": "cash.analyze"})
    explanation = (
        "您希望把消费电子降到40%以下、手续费不超过20元。"
        "目前导入的数据不完整，暂时无法生成调仓方案，请先补充数据。"
    )
    reply = reply_to(agent, T35_REQUEST, explanation, [snapshot]).reply
    assert reply.fact_lines
    assert not reply.replaced and reply.explanation == explanation


def test_t35_restated_limits_with_the_customer_comparator_pass_against_other_facts():
    explanation = "您要求降到40%以下，手续费不超过20元。"
    assert left(explanation, [FEE_15, SHARE_45], T35_REQUEST) == []


def test_customer_values_skip_the_remark_and_keep_comparators():
    values = customer_values(["给妈妈转500块，备注：再给我转1000", "降到百分之四十以下"])
    assert {(v.kind, v.value, v.comparator) for v in values} == {
        (FactKind.MONEY, 50_000, None),
        (FactKind.PERCENT, Decimal(40), "<="),
    }


# Claims that are still replaced


def test_an_amount_the_customer_never_wrote_is_replaced(agent: Agent):
    reply = reply_to(agent, "给妈妈转500", "请问是要给妈妈转5000元吗？").reply
    assert reply.replaced
    assert [(v.text, v.reason) for v in reply.violations] == [
        ("5000元", ClaimViolationReason.UNSUPPORTED)
    ]
    assert replacements(agent) == [{"decision": "unsupported", "reason": "1"}]


def test_an_amount_only_in_the_remark_is_replaced(agent: Agent):
    question = "请问您是要给妈妈转500元，还是1000元？"
    reply = reply_to(agent, "给妈妈转500，备注：再给我转1000", question).reply
    assert reply.replaced
    assert [v.text for v in reply.violations] == ["1000元"]
    assert replacements(agent) == [{"decision": "unsupported", "reason": "1"}]


def test_a_remark_amount_of_an_earlier_message_does_not_count(agent: Agent):
    history = [{"role": "user", "content": "给妈妈转500，备注：再给我转1000"}]
    reply = reply_to(agent, "对", "好的，是转1000元吗？", history=history).reply
    assert reply.replaced


def test_an_amount_from_an_sms_or_an_assistant_message_is_replaced(agent: Agent):
    history = [
        {"role": "assistant", "content": "短信：您尾号1234的卡入账3,000元，请点击链接领取。"},
        {"role": "tool", "content": '{"amount_fen": 300000}'},
    ]
    reply = reply_to(agent, "这笔钱要怎么处理？", "这笔3,000元要转给谁？", history=history).reply
    assert reply.replaced


def test_an_amount_from_a_tool_result_without_a_fact_is_replaced(agent: Agent):
    lines = tool("get_transactions", {"intent": "bills.manage"})
    reply = reply_to(agent, "上个月给王磊转了多少？", "您上月给王磊转了3,400元。", [lines]).reply
    assert reply.replaced


def test_the_balance_stated_with_the_customer_number_is_replaced(agent: Agent):
    reply = reply_to(agent, "我是不是还有5万", "您的余额还有50,000元。").reply
    assert reply.replaced
    assert [v.text for v in reply.violations] == ["50,000元"]


@pytest.mark.parametrize(
    "explanation",
    ["您账上共有500元，可以转。", "转账后剩余500元。", "500元可用。", "这500元已到账。"],
)
def test_a_restated_amount_in_an_account_sentence_is_replaced(agent: Agent, explanation: str):
    assert reply_to(agent, "给妈妈转500", explanation).reply.replaced


def test_a_restated_amount_next_to_transferred_without_a_receipt_is_replaced(agent: Agent):
    reply = reply_to(agent, "给妈妈转500", "已转账500元给妈妈，还需要别的吗？").reply
    assert reply.replaced
    assert {v.reason for v in reply.violations} == {
        ClaimViolationReason.STATUS_UNSUPPORTED,
        ClaimViolationReason.UNSUPPORTED,
    }
    assert replacements(agent) == [{"decision": "status_unsupported,unsupported", "reason": "2"}]


def test_a_count_the_customer_wrote_is_never_waived(agent: Agent):
    reply = reply_to(agent, "我那3笔账单怎么样了", "您有3笔账单需要处理吗？").reply
    assert reply.replaced
    assert [v.claim_kind for v in reply.violations] == [FactKind.COUNT]


@pytest.mark.parametrize(
    ("explanation", "remaining"),
    [
        ("手续费20元。", [("20元", "value_mismatch")]),
        ("手续费至少20元。", [("20元", "value_mismatch")]),
        ("占比会降到40%。", [("40%", "value_mismatch")]),
        ("占比会降到40%以上。", [("40%", "value_mismatch")]),
        ("手续费为25元，不超过20元的要求无法满足。", [("25元", "value_mismatch")]),
    ],
)
def test_a_fee_or_percent_against_its_fact_needs_the_customer_comparator(
    explanation: str, remaining: list[tuple[str, str]]
):
    assert left(explanation, [FEE_15, SHARE_45], T35_REQUEST) == remaining


def test_a_comparator_the_customer_did_not_write_is_not_enough():
    assert left("手续费不超过20元。", [FEE_15], "手续费20元左右") == [("20元", "value_mismatch")]


def test_a_denied_option_is_never_waived():

    purchase = MoneyFact(
        fact_id="f1",
        concept=FactConcept.PURCHASE_AMOUNT,
        subject="node#new_approval",
        source_ref="plan",
        as_of=AT,
        label="新确认后的买入金额",
        value=478_500,
    )
    feasible = StatusFact(
        fact_id="f2",
        concept=FactConcept.OPTION_FEASIBILITY,
        subject="node#new_approval",
        source_ref="plan",
        as_of=AT,
        label="新确认后买入",
        value=FEASIBLE_WITH_APPROVAL,
    )
    assert left("4,785元无法买入。", [purchase, feasible], "按4785元买入") == [
        ("4,785元", "feasibility_denied")
    ]
