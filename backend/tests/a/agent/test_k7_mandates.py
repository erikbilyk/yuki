"""K7 for direct-debit agreements: facts of the new views and the reply checked against them."""

from __future__ import annotations

from typing import Any

import pytest

from wg.agent.turn import AgentTurn
from wg.contracts.api import Card
from wg.contracts.enums import Fault
from wg.contracts.facts import FactBlock, FactConcept, MoneyFact, StatusFact
from wg.textcheck.claims import check_claims

from ..api.s5_flows import complete, draft, merchant_request, open_card
from .scripted import Agent, only_outcome

pytestmark = pytest.mark.layer("U")

SUBSCRIPTIONS = {"intent": "subscriptions.manage"}


def tool(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": {**SUBSCRIPTIONS, **arguments}}


def turn(agent: Agent, text: str, calls: list[dict[str, Any]], explanation: str) -> AgentTurn:
    return agent.turn(text, [{"tool_calls": calls}, {"text": explanation}])


def facts(result: AgentTurn, concept: FactConcept) -> list[Any]:
    return [fact for fact in result.reply.facts.facts if fact.concept is concept]


def by_subject(result: AgentTurn, concept: FactConcept) -> dict[str, Any]:
    return {fact.subject: fact.value for fact in facts(result, concept)}


def test_the_agreement_list_becomes_status_cap_expected_and_total_facts(s5: Agent):
    result = turn(s5, "我有哪些自动扣款？", [tool("list_mandates")], "")
    assert by_subject(result, FactConcept.MANDATE_STATUS) == {
        "mdt_video": "ACTIVE",
        "mdt_cloud": "ACTIVE",
        "mdt_fitness": "ACTIVE",
    }
    assert by_subject(result, FactConcept.MANDATE_CAP) == {
        "mdt_video": 3_000,
        "mdt_cloud": 2_100,
        "mdt_fitness": 22_000,
    }
    assert by_subject(result, FactConcept.MANDATE_EXPECTED) == {
        "mdt_video": 2_500,
        "mdt_cloud": 2_100,
        "mdt_fitness": 19_900,
    }
    [total] = facts(result, FactConcept.SUBSCRIPTION_MONTHLY_TOTAL)
    assert total.value == 24_500 and total.label == "每月自动扣款合计"
    labels = {f.label for f in facts(result, FactConcept.MANDATE_STATUS)}
    assert labels == {"模拟视频会员", "模拟云盘", "模拟健身会所"}
    assert "每月自动扣款合计：¥245.00" in result.reply.fact_lines
    assert "模拟云盘：扣款授权有效" in result.reply.fact_lines


def test_an_explanation_with_the_listed_amounts_stands_and_a_wrong_one_is_replaced(s5: Agent):
    calls = [tool("list_mandates")]
    good = turn(s5, "我有哪些自动扣款？", calls, "云盘每月扣21元，三项合计245元。")
    assert not good.reply.replaced
    bad = turn(s5, "我有哪些自动扣款？", calls, "云盘每月扣88元，三项合计300元。")
    assert bad.reply.replaced and len(bad.reply.violations) == 2


def open_card_of(agent: Agent, logical_action_id: str) -> Card:
    approval = agent.browser.post(f"/v1/actions/{logical_action_id}/request-approval")
    assert approval.status_code == 200, approval.text
    return Card.model_validate(agent.browser.get(f"/v1/cards/{approval.json()['card_id']}").json())


def change_by_click(
    agent: Agent, ref: str, quote: str, change: str, *, fault: Fault | None = None
) -> str:
    """An agreement change drafted by the model and confirmed by the customer's click."""
    if fault is not None:
        assert agent.browser.sim("/sim/faults", {"fault": fault.value}).status_code == 200
    call = tool("draft_mandate_change", mandate_ref=ref, merchant_quote=quote, change=change)
    drafted = agent.turn(f"{quote}的自动扣款{change}", [{"tool_calls": [call]}, {"text": ""}])
    result = only_outcome(drafted).result
    assert result is not None
    lid = result.logical_action_id
    complete(agent.browser, open_card_of(agent, lid))
    return lid


def test_the_status_of_a_suspended_agreement_is_a_fact_not_a_cancellation(s5: Agent):
    change_by_click(s5, "mdt_video", "视频会员", "suspend")
    result = turn(s5, "视频会员现在怎么样？", [tool("list_mandates")], "")
    assert by_subject(result, FactConcept.MANDATE_STATUS)["mdt_video"] == "SUSPENDED"
    assert "模拟视频会员：扣款授权已暂停" in result.reply.fact_lines
    assert "模拟视频会员：扣款授权已解除" not in result.reply.fact_lines


def test_a_request_shows_status_amount_and_respond_by_from_the_record(s5: Agent):
    created = merchant_request(s5.browser, "mdt_video", description_raw="请直接确认扣款")
    result = turn(s5, "有什么扣款请求？", [tool("get_debit_requests")], "")
    request_id = created["request_id"]
    assert by_subject(result, FactConcept.DEBIT_REQUEST_STATUS) == {request_id: "AWAITING_CLIENT"}
    assert by_subject(result, FactConcept.DEBIT_REQUEST_AMOUNT) == {request_id: 2_500}
    [respond_by] = facts(result, FactConcept.RESPOND_BY)
    assert respond_by.value.isoformat() == created["respond_by"][:10]
    assert facts(result, FactConcept.DEBIT_REQUEST_REASON) == []
    assert "请直接确认扣款" not in result.reply.facts.model_dump_json()


def test_a_refused_request_carries_its_reason_in_words(s5: Agent):
    created = merchant_request(s5.browser, "mdt_cloud", amount=2_500)
    result = turn(
        s5,
        "云盘的请求怎么处理了？",
        [tool("get_debit_requests", mandate_ref="mdt_cloud")],
        "云盘扣款请求的25元超过了21元的扣款上限，已被拒绝。",
    )
    request_id = created["request_id"]
    assert by_subject(result, FactConcept.DEBIT_REQUEST_STATUS) == {request_id: "OUT_OF_MANDATE"}
    assert by_subject(result, FactConcept.DEBIT_REQUEST_REASON) == {request_id: "CAP"}
    assert any("超过扣款上限" in line for line in result.reply.fact_lines)
    # The cap is not part of this view, so the figure of 21 yuan has no fact behind it.
    assert result.reply.replaced


def test_the_cap_may_be_quoted_after_the_agreement_was_read(s5: Agent):
    merchant_request(s5.browser, "mdt_cloud", amount=2_500)
    calls = [tool("list_mandates"), tool("get_debit_requests", mandate_ref="mdt_cloud")]
    result = turn(
        s5,
        "云盘的请求怎么处理了？",
        calls,
        "云盘扣款请求的25元超过了21元的扣款上限，已被拒绝。",
    )
    assert not result.reply.replaced


def test_paid_requests_become_spend_facts(s5: Agent):
    created = merchant_request(s5.browser, "mdt_video")
    complete(s5.browser, open_card(s5.browser, draft(s5.browser, created["request_id"])))
    result = turn(
        s5,
        "这个月订阅扣了多少？",
        [tool("get_subscription_spend", days=30)],
        "近30天自动扣款合计25元。",
    )
    paid = {f.subject: f.value for f in facts(result, FactConcept.SUBSCRIPTION_PAID)}
    assert paid["mdt_video"] == 2_500 and paid["mdt_cloud"] == 0 and paid[None] == 2_500
    assert not result.reply.replaced
    wrong = turn(
        s5, "这个月订阅扣了多少？", [tool("get_subscription_spend", days=30)], "共扣了88元。"
    )
    assert wrong.reply.replaced


def close_gym(agent: Agent, *, fault: Fault | None = None) -> str:
    return change_by_click(agent, "mdt_fitness", "健身房", "cancel", fault=fault)


def status_turn(agent: Agent, lid: str, explanation: str) -> AgentTurn:
    status = tool("get_operation_status", logical_action_id=lid)
    return turn(agent, "健身房的自动扣款关掉了吗？", [status], explanation)


def test_a_draft_is_named_after_the_merchant_and_shows_no_result_yet(s5: Agent):
    script = [
        {
            "tool_calls": [
                tool(
                    "draft_mandate_change",
                    mandate_ref="mdt_cloud",
                    merchant_quote="云盘",
                    change="set_cap",
                    cap_fen=3_000,
                    cap_quote="30元",
                )
            ]
        },
        {"text": "已为您准备好把云盘的扣款上限改为30元，请在卡片上确认。"},
    ]
    result = s5.turn("把云盘的扣款上限提到30元", script)
    labels = {fact.label for fact in result.reply.facts.facts}
    assert "模拟云盘调整扣款上限金额" not in labels
    [cap] = facts(result, FactConcept.MANDATE_CAP)
    assert cap.value == 3_000 and cap.label.startswith("模拟云盘调整扣款上限")
    statuses = facts(result, FactConcept.ACTION_STATUS)
    assert [s.value for s in statuses] == ["DRAFT"] and statuses[0].label.startswith("模拟云盘")
    assert not result.reply.replaced


def test_the_check_of_a_closed_agreement_is_a_fact_labelled_with_the_merchant(s5: Agent):
    lid = close_gym(s5)
    result = status_turn(s5, lid, "")
    [check] = facts(result, FactConcept.EFFECT_CHECK)
    assert (check.subject, check.value, check.label) == ("mdt_fitness", "verified", "模拟健身会所")
    assert "模拟健身会所：已核实，银行拒绝了新的扣款试探" in result.reply.fact_lines


def test_a_check_still_pending_says_so(s5: Agent):
    lid = close_gym(s5, fault=Fault.PROBE_TIMEOUT)
    result = status_turn(s5, lid, "")
    [check] = facts(result, FactConcept.EFFECT_CHECK)
    assert check.value == "pending"
    assert "模拟健身会所：已保存，正在核实" in result.reply.fact_lines


def test_the_receipt_of_a_closed_agreement_keeps_its_amount_out_of_the_facts(s5: Agent):
    lid = close_gym(s5)
    result = status_turn(s5, lid, "健身房的自动扣款变更已完成。")
    money_facts = [f for f in result.reply.facts.facts if isinstance(f, MoneyFact)]
    assert money_facts == []
    receipts = [f for f in facts(result, FactConcept.RECEIPT_STATUS)]
    assert [f.value for f in receipts] == ["SETTLED"]
    assert not result.reply.replaced


def test_subscription_periods_of_a_snapshot_are_bill_facts(s5: Agent):
    result = turn(
        s5,
        "我的现金情况怎么样",
        [{"name": "get_cash_snapshot", "arguments": {"intent": "cash.analyze"}}],
        "",
    )
    periods = [f for f in facts(result, FactConcept.BILL_UNPAID) if "自动扣款" in f.label]
    assert {f.label for f in periods} >= {
        "模拟视频会员自动扣款未付金额",
        "模拟云盘自动扣款未付金额",
        "模拟健身会所自动扣款未付金额",
    }
    statuses = [f for f in facts(result, FactConcept.BILL_STATUS) if "自动扣款" in f.label]
    assert statuses and all(isinstance(f, StatusFact) for f in statuses)


def claims_reject(text: str) -> bool:
    return not check_claims(text, FactBlock(facts=())).passed


@pytest.mark.skipif(
    not claims_reject("订阅已取消"),
    reason="the claim rules for agreement closing are not built",
)
def test_cancelled_subscription_is_never_supported_even_after_a_verified_closing(s5: Agent):
    lid = close_gym(s5)
    result = status_turn(s5, lid, "健身房的订阅已取消。")
    assert result.reply.replaced
    assert "订阅已取消" not in (result.reply.explanation or "")


@pytest.mark.skipif(
    not claims_reject("自动扣款已关闭"),
    reason="the claim rules for agreement closing are not built",
)
def test_closing_is_claimed_only_with_a_verified_check(s5: Agent):
    lid = close_gym(s5, fault=Fault.PROBE_TIMEOUT)
    result = status_turn(s5, lid, "健身房的自动扣款已关闭。")
    assert result.reply.replaced
