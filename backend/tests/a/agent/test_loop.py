"""The agent loop: model turns, the 8-call budget and what the model is shown."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wg.agent.llm import ModelOutputError, build_adapter
from wg.agent.loop import MAX_TOOL_CALLS, run_turn
from wg.agent.prompt import load_system_prompt
from wg.agent.registry import ToolContext, ToolRegistry
from wg.contracts.enums import Channel
from wg.contracts.errors import ErrorCode
from wg.settings import Settings

from .conftest import ScriptedProvider, reply, tool_call

pytestmark = pytest.mark.layer("U")

BALANCE = {"intent": "balance.query"}


def test_a_turn_without_tool_calls_returns_the_text(
    adapter_factory, registry: ToolRegistry, context: ToolContext
):
    provider = ScriptedProvider([reply(text="您好，请问需要什么帮助？")])
    result = run_turn(adapter_factory(provider), registry, context, "你好")
    assert result.explanation == "您好，请问需要什么帮助？"
    assert result.outcomes == () and result.model_calls == 1 and not result.limit_reached
    messages, tools = provider.calls[0]
    assert messages[0]["role"] == "system" and messages[-1] == {"role": "user", "content": "你好"}
    assert len(tools) == 27


def test_tool_results_go_back_to_the_model_before_the_final_text(
    adapter_factory, registry: ToolRegistry, context: ToolContext
):
    provider = ScriptedProvider(
        [reply(tool_call("get_accounts", BALANCE)), reply(text="主账户余额见上。")]
    )
    result = run_turn(adapter_factory(provider), registry, context, "我有多少钱")
    [outcome] = result.outcomes
    assert outcome.ok and result.model_calls == 2
    messages, _ = provider.calls[1]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool"]
    assert messages[2]["tool_calls"][0]["function"]["name"] == "get_accounts"
    assert messages[3]["tool_call_id"] == "call_0"
    assert json.loads(messages[3]["content"]) == json.loads(outcome.payload())


def test_a_refused_call_is_shown_to_the_model_as_an_error(
    adapter_factory, registry: ToolRegistry, context: ToolContext
):
    bad = tool_call("draft_transfer", {"intent": "transfer.create", "user_confirmed": True})
    provider = ScriptedProvider([reply(bad), reply(text="无法完成。")])
    result = run_turn(adapter_factory(provider), registry, context, "转账")
    assert result.outcomes[0].error is not None
    sent = json.loads(provider.calls[1][0][-1]["content"])
    assert sent["code"] == ErrorCode.UNSUPPORTED_REQUEST.value


def test_at_most_eight_tool_calls_run_in_a_turn(
    adapter_factory, registry: ToolRegistry, context: ToolContext
):
    calls = [tool_call("get_accounts", BALANCE, index) for index in range(MAX_TOOL_CALLS + 2)]
    provider = ScriptedProvider([reply(*calls), reply(text="已查询。")])
    result = run_turn(adapter_factory(provider), registry, context, "查一下")
    executed = [o for o in result.outcomes if o.arguments is not None]
    skipped = [o for o in result.outcomes if o.arguments is None]
    assert len(executed) == MAX_TOOL_CALLS and len(skipped) == 2
    assert all(o.error and o.error.detail == {"reason": "tool_call_limit"} for o in skipped)
    assert result.limit_reached and result.explanation == "已查询。"
    messages, tools = provider.calls[1]
    assert tools == []
    assert sum(m["role"] == "tool" for m in messages) == MAX_TOOL_CALLS + 2


def test_a_model_that_keeps_calling_tools_is_stopped_after_eight(
    adapter_factory, registry: ToolRegistry, context: ToolContext
):
    replies = [reply(tool_call("get_accounts", BALANCE, i)) for i in range(MAX_TOOL_CALLS)]
    replies.append(reply(text="完成"))
    provider = ScriptedProvider(replies)
    result = run_turn(adapter_factory(provider), registry, context, "查一下")
    assert len(result.outcomes) == MAX_TOOL_CALLS and result.model_calls == MAX_TOOL_CALLS + 1
    assert provider.calls[-1][1] == []


def test_the_prompt_version_is_recorded_in_every_call(
    adapter_factory, registry: ToolRegistry, context: ToolContext, tmp_path: Path
):
    provider = ScriptedProvider([reply(tool_call("get_accounts", BALANCE)), reply(text="好")])
    run_turn(adapter_factory(provider), registry, context, "余额")
    lines = (tmp_path / "llm_calls.jsonl").read_text(encoding="utf-8").splitlines()
    versions = {json.loads(line)["prompt_version"] for line in lines}
    assert versions == {load_system_prompt().version} and len(lines) == 2
    assert all(json.loads(line)["tool_schema_version"] for line in lines)


def test_the_system_prompt_is_chinese_and_forbids_claiming_execution():
    prompt = load_system_prompt()
    assert prompt.version.startswith("system_zh-") and not prompt.text.startswith("version:")
    for phrase in ("工具", "已转账", "request_approval", "不要猜测"):
        assert phrase in prompt.text


def test_a_stub_script_drives_a_whole_turn(
    registry: ToolRegistry, context: ToolContext, llm_settings: Settings, tmp_path: Path
):
    script = tmp_path / "script.yaml"
    script.write_text(
        "- tool_calls:\n"
        "    - name: draft_transfer\n"
        "      arguments:\n"
        "        intent: transfer.create\n"
        "        payee_ref: pay_mom\n"
        "        payee_quote: 妈妈\n"
        "        amount_fen: 50000\n"
        "        amount_quote: 500元\n"
        "- text: 已为您准备好转账草稿，请确认。\n",
        encoding="utf-8",
    )
    settings = Settings(
        **{**llm_settings.__dict__, "llm_provider": "stub", "llm_stub_script": str(script)}
    )
    adapter = build_adapter(settings, log_path=tmp_path / "calls.jsonl")
    result = run_turn(adapter, registry, context, "给妈妈转500元")
    assert result.outcomes[0].ok and "草稿" in result.explanation
    with registry.db.read() as conn:
        assert conn.execute("SELECT status FROM actions").fetchone()["status"] == "DRAFT"


def test_a_model_error_ends_the_turn(adapter_factory, registry: ToolRegistry, context: ToolContext):
    class Failing(ScriptedProvider):
        def complete(self, messages, tools):
            raise ModelOutputError("invalid_tool_arguments")

    with pytest.raises(ModelOutputError):
        run_turn(adapter_factory(Failing()), registry, context, "你好")


def test_the_im_channel_sees_the_same_tools(registry: ToolRegistry):
    im = ToolContext(customer_id="cus_main", channel=Channel.IM)
    assert len(registry.schemas(im)) == 27
