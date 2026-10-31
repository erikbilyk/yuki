"""Tool calls the model writes as text after the budget is spent never reach the customer.

The markup is the form DeepSeek wrote in live runs when the last call had no tools. It is
removed in the loop; K7 then checks whatever text remains, as for any other explanation.
"""

from __future__ import annotations

import pytest

from wg.agent.loop import MAX_TOOL_CALLS, run_turn, strip_tool_markup
from wg.agent.registry import ToolContext, ToolRegistry
from wg.core.k7_verify import REPLACED_EVENT

from .conftest import ScriptedProvider, reply, tool_call
from .scripted import Agent, ReadingModel

pytestmark = pytest.mark.layer("U")

BALANCE = {"intent": "balance.query"}
BAR = "｜｜"
NO_DIGITS = (
    f"<{BAR}DSML{BAR} calls>\n"
    f'<{BAR}DSML{BAR} invoke name="get_bills">\n'
    f'<{BAR}DSML{BAR} parameter name="intent" string="true">bills.manage'
    f"</{BAR}DSML{BAR} parameter>\n"
    f"</{BAR}DSML{BAR} invoke>\n"
    f"</{BAR}DSML{BAR} calls>"
)
WITH_DIGITS = (
    f"<{BAR}DSML{BAR} calls>\n"
    f'<{BAR}DSML{BAR} invoke name="compose_plan">\n'
    f'<{BAR}DSML{BAR} parameter name="items" string="false">'
    '[{"kind": "PAY_BILL", "note": "房租3400元，到期日2026-11-04支付"}]'
    f"</{BAR}DSML{BAR} parameter>\n"
    f"</{BAR}DSML{BAR} invoke>\n"
    f"</{BAR}DSML{BAR} calls>"
)
SINGLE_BARS = (
    "<｜DSML｜function_calls>\n"
    '<｜DSML｜invoke name="get_bills">\n'
    '<｜DSML｜parameter name="intent" string="true">bills.manage</｜DSML｜parameter>\n'
    "</｜DSML｜invoke>\n"
    "</｜DSML｜function_calls>"
)


def spent_budget(final_text: str) -> ReadingModel:
    """Eight reads in one reply, then the last call without tools writes `final_text`."""
    reads = [("get_accounts", BALANCE)] * MAX_TOOL_CALLS
    return ReadingModel([lambda _: reads], final_text=final_text)


def replacements(agent: Agent) -> int:
    return len(agent.rows("SELECT event FROM audit_log WHERE event = ?", REPLACED_EVENT))


@pytest.mark.parametrize("markup", [NO_DIGITS, WITH_DIGITS, SINGLE_BARS])
def test_markup_is_removed_and_the_text_around_it_kept(markup: str):
    text = f"保险这一项随后再为您补上。\n\n{markup}\n\n请先确认上面的卡片。"
    assert strip_tool_markup(text) == "保险这一项随后再为您补上。\n\n请先确认上面的卡片。"


def test_markup_cut_off_before_its_closing_tag_is_removed_to_the_end():
    text = f"我先把方案合起来。\n\n{WITH_DIGITS[:120]}"
    assert strip_tool_markup(text) == "我先把方案合起来。"


def test_a_stray_markup_tag_is_removed():
    assert strip_tool_markup(f"好的。</{BAR}DSML{BAR} calls>") == "好的。"


@pytest.mark.parametrize(
    "text",
    [
        "已为您准备好，等待您确认。",
        "  房租 3400 元｜11月4日到期 <请确认>\n\n\n\n保险 | 11月9日到期  \n",
        "DSML 不是工具调用，这句话原样保留。\n",
        "",
    ],
)
def test_plain_text_is_not_altered(text: str):
    assert strip_tool_markup(text) == text


def test_the_loop_removes_markup_from_the_last_call_after_the_budget(
    adapter_factory, registry: ToolRegistry, context: ToolContext
):
    calls = [tool_call("get_accounts", BALANCE, index) for index in range(MAX_TOOL_CALLS)]
    provider = ScriptedProvider([reply(*calls), reply(text=f"余额已查询。\n\n{NO_DIGITS}")])
    result = run_turn(adapter_factory(provider), registry, context, "查一下")
    assert result.limit_reached and provider.calls[-1][1] == []
    assert result.explanation == "余额已查询。"
    # The call written as text did not run.
    assert [o.name for o in result.outcomes] == ["get_accounts"] * MAX_TOOL_CALLS


def test_markup_without_digits_does_not_reach_the_customer(agent: Agent):
    done = agent.play("查一下余额", spent_budget(f"余额已为您查好。\n\n{NO_DIGITS}"))
    assert done.turn.limit_reached
    assert done.reply.explanation == "余额已为您查好。" and not done.reply.replaced
    assert replacements(agent) == 0


def test_only_markup_leaves_no_explanation_and_the_fact_lines_stand(agent: Agent):
    done = agent.play("查一下余额", spent_budget(NO_DIGITS))
    assert done.turn.explanation == ""
    assert done.reply.explanation is None and not done.reply.replaced
    assert done.reply.fact_lines


def test_markup_with_digits_is_removed_and_the_rest_is_still_checked(agent: Agent):
    claim = "房租9999元已为您支付。"
    done = agent.play("查一下余额", spent_budget(f"{claim}\n\n{WITH_DIGITS}"))
    assert done.turn.explanation == claim
    # K7 replaces the false claim that remains, as it would without the markup.
    assert done.reply.replaced and done.reply.explanation != claim
    assert "DSML" not in (done.reply.explanation or "")
    assert replacements(agent) == 1


def test_a_supported_remainder_passes_k7_once_the_markup_is_gone(agent: Agent):
    done = agent.play("查一下余额", spent_budget(f"余额见上。\n\n{WITH_DIGITS}"))
    assert done.reply.explanation == "余额见上。" and not done.reply.replaced
    assert replacements(agent) == 0
