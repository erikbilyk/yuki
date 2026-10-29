"""The model adapter: call records, spend guard, retries and malformed output."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from wg.agent.llm import (
    DeepSeekProvider,
    LlmError,
    ModelOutputError,
    PriceTable,
    ProviderError,
    SpendCapReached,
    StubProvider,
    StubScriptExhausted,
    Usage,
    build_adapter,
)
from wg.settings import Settings

from .conftest import ScriptedProvider, reply

pytestmark = pytest.mark.layer("U")

MESSAGES = [{"role": "user", "content": "你好，尾号4321的卡"}]


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def completion(message: dict, usage: dict | None = None) -> dict:
    return {
        "model": "deepseek-flash-1",
        "choices": [{"message": message}],
        "usage": usage
        or {
            "prompt_tokens": 100,
            "prompt_cache_hit_tokens": 40,
            "prompt_cache_miss_tokens": 60,
            "completion_tokens": 10,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


def provider_with(settings: Settings, handler) -> DeepSeekProvider:
    return DeepSeekProvider(
        settings, transport=httpx.MockTransport(handler), sleep=lambda seconds: None
    )


def test_price_table_cost_is_rounded_up_to_the_micro_yuan():
    prices = PriceTable.parse("0.04/2/8")
    usage = Usage(cache_hit_tokens=40, cache_miss_tokens=60, completion_tokens=10)
    assert prices.cost(usage) == Decimal("0.000202")
    assert prices.cost(Usage()) == Decimal("0")
    with pytest.raises(ValueError):
        PriceTable.parse("1/2")
    with pytest.raises(ValueError):
        PriceTable.parse("a/b/c")


def test_a_call_appends_one_record_without_message_text_or_key(
    adapter_factory, llm_settings: Settings, tmp_path: Path
):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=completion({"content": "收到"}))

    adapter = adapter_factory(ScriptedProvider(), llm_settings)
    adapter.provider = provider_with(llm_settings, handler)
    result = adapter.complete(MESSAGES, [], prompt_version="p-1")

    assert result.text == "收到" and result.returned_model == "deepseek-flash-1"
    [record] = records(tmp_path / "llm_calls.jsonl")
    assert record["requested_model"] == "deepseek-flash"
    assert record["returned_model"] == "deepseek-flash-1"
    assert record["thinking"] == "disabled" and record["prompt_version"] == "p-1"
    assert record["usage"] == {
        "cache_hit_tokens": 40,
        "cache_miss_tokens": 60,
        "completion_tokens": 10,
        "reasoning_tokens": 3,
    }
    assert record["cost_cny"] == "0.000202" and record["error"] is None
    raw = (tmp_path / "llm_calls.jsonl").read_text(encoding="utf-8")
    assert "4321" not in raw and "收到" not in raw and llm_settings.llm_api_key not in raw
    [request] = seen
    assert request.headers["authorization"] == f"Bearer {llm_settings.llm_api_key}"
    assert json.loads(request.content)["thinking"] == {"type": "disabled"}


def test_the_http_client_ignores_the_proxy_environment(
    monkeypatch: pytest.MonkeyPatch, llm_settings: Settings
):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    provider = DeepSeekProvider(llm_settings)
    assert provider._client.trust_env is False


def test_invalid_json_arguments_become_a_model_error(
    adapter_factory, llm_settings: Settings, tmp_path: Path
):
    message = {
        "content": "",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_accounts", "arguments": "{"}}
        ],
    }
    adapter = adapter_factory(ScriptedProvider(), llm_settings)
    adapter.provider = provider_with(
        llm_settings, lambda request: httpx.Response(200, json=completion(message))
    )
    with pytest.raises(ModelOutputError) as caught:
        adapter.complete(MESSAGES, [], prompt_version="p-1")
    assert caught.value.reason == "invalid_tool_arguments"
    [record] = records(tmp_path / "llm_calls.jsonl")
    assert record["error"] == "invalid_tool_arguments"
    assert record["usage"]["completion_tokens"] == 10 and record["cost_cny"] == "0.000202"


@pytest.mark.parametrize("arguments", ["[1]", '"x"', "null"])
def test_arguments_that_are_not_an_object_are_refused(
    adapter_factory, llm_settings: Settings, arguments: str
):
    message = {
        "tool_calls": [{"id": "c1", "function": {"name": "get_accounts", "arguments": arguments}}]
    }
    adapter = adapter_factory(ScriptedProvider(), llm_settings)
    adapter.provider = provider_with(
        llm_settings, lambda request: httpx.Response(200, json=completion(message))
    )
    with pytest.raises(ModelOutputError):
        adapter.complete(MESSAGES, [], prompt_version="p-1")


def test_valid_tool_call_arguments_are_parsed_without_change(
    adapter_factory, llm_settings: Settings
):
    raw = '{"intent": "balance.query", "account_id": "acc_main"}'
    message = {
        "tool_calls": [
            {"id": "c1", "function": {"name": "get_balance", "arguments": raw}},
        ]
    }
    adapter = adapter_factory(ScriptedProvider(), llm_settings)
    adapter.provider = provider_with(
        llm_settings, lambda request: httpx.Response(200, json=completion(message))
    )
    [call] = adapter.complete(MESSAGES, [], prompt_version="p-1").tool_calls
    assert (call.call_id, call.name, call.arguments_json) == ("c1", "get_balance", raw)
    assert call.arguments == {"intent": "balance.query", "account_id": "acc_main"}


def test_a_transient_error_is_retried_once(adapter_factory, llm_settings: Settings):
    statuses = [503, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status != 200:
            return httpx.Response(status, text="busy")
        return httpx.Response(200, json=completion({"content": "好"}))

    adapter = adapter_factory(ScriptedProvider(), llm_settings)
    adapter.provider = provider_with(llm_settings, handler)
    assert adapter.complete(MESSAGES, [], prompt_version="p-1").text == "好"
    assert statuses == []


def test_a_second_transient_error_is_reported_and_recorded(
    adapter_factory, llm_settings: Settings, tmp_path: Path
):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(502, text="bad gateway")

    adapter = adapter_factory(ScriptedProvider(), llm_settings)
    adapter.provider = provider_with(llm_settings, handler)
    with pytest.raises(ProviderError) as caught:
        adapter.complete(MESSAGES, [], prompt_version="p-1")
    assert len(calls) == 2 and caught.value.status == 502
    assert records(tmp_path / "llm_calls.jsonl")[0]["error"] == "http_502"


def test_a_timeout_is_retried_then_recorded(
    adapter_factory, llm_settings: Settings, tmp_path: Path
):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    adapter = adapter_factory(ScriptedProvider(), llm_settings)
    adapter.provider = provider_with(llm_settings, handler)
    with pytest.raises(ProviderError) as caught:
        adapter.complete(MESSAGES, [], prompt_version="p-1")
    assert len(calls) == 2 and caught.value.reason == "timeout"
    assert records(tmp_path / "llm_calls.jsonl")[0]["error"] == "timeout"


def test_a_rejected_request_is_not_retried_and_its_body_hides_the_key(
    adapter_factory, llm_settings: Settings
):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, text=f"bad key {llm_settings.llm_api_key}")

    adapter = adapter_factory(ScriptedProvider(), llm_settings)
    adapter.provider = provider_with(llm_settings, handler)
    with pytest.raises(ProviderError) as caught:
        adapter.complete(MESSAGES, [], prompt_version="p-1")
    assert len(calls) == 1 and caught.value.status == 401
    assert llm_settings.llm_api_key not in caught.value.body
    assert llm_settings.llm_api_key not in str(caught.value)


def test_spend_guard_refuses_once_the_recorded_total_reaches_the_cap(
    adapter_factory, tmp_path: Path
):
    expensive = Usage(cache_miss_tokens=1_000_000)  # CNY 2 at the default prices; the cap is 1
    provider = ScriptedProvider([reply(text="a", usage=expensive), reply(text="b")])
    adapter = adapter_factory(provider)

    adapter.complete(MESSAGES, [], prompt_version="p-1")
    assert adapter.recorded_spend() == Decimal("2")
    with pytest.raises(SpendCapReached):
        adapter.complete(MESSAGES, [], prompt_version="p-1")

    assert len(provider.calls) == 1
    refused = records(tmp_path / "llm_calls.jsonl")[-1]
    assert refused["error"] == "spend_cap" and refused["cost_cny"] == "0.000000"


def test_spend_below_the_cap_still_allows_calls(adapter_factory):
    cheap = Usage(cache_miss_tokens=100_000)  # CNY 0.2
    provider = ScriptedProvider([reply(text="a", usage=cheap) for _ in range(3)])
    adapter = adapter_factory(provider)
    for _ in range(3):
        adapter.complete(MESSAGES, [], prompt_version="p-1")
    assert adapter.recorded_spend() == Decimal("0.6")


def test_a_model_call_inside_a_unit_of_work_is_refused(client, adapter_factory):
    provider = ScriptedProvider([reply(text="a")])
    adapter = adapter_factory(provider)
    with client.app.state.db.unit_of_work("test"):
        with pytest.raises(RuntimeError):
            adapter.complete(MESSAGES, [], prompt_version="p-1")
    assert provider.calls == []


def test_stub_provider_replays_the_script_in_order(tmp_path: Path, llm_settings: Settings):
    script = tmp_path / "script.yaml"
    script.write_text(
        "- tool_calls:\n"
        "    - name: get_accounts\n"
        "      arguments: {intent: balance.query}\n"
        "- text: 余额如上\n",
        encoding="utf-8",
    )
    settings = Settings(
        **{**llm_settings.__dict__, "llm_provider": "stub", "llm_stub_script": str(script)}
    )
    adapter = build_adapter(settings, log_path=tmp_path / "calls.jsonl")

    first = adapter.complete(MESSAGES, [], prompt_version="p-1")
    assert [(c.name, c.arguments) for c in first.tool_calls] == [
        ("get_accounts", {"intent": "balance.query"})
    ]
    assert adapter.complete(MESSAGES, [], prompt_version="p-1").text == "余额如上"
    with pytest.raises(StubScriptExhausted):
        adapter.complete(MESSAGES, [], prompt_version="p-1")
    assert [r["provider"] for r in records(tmp_path / "calls.jsonl")] == ["stub"] * 3


def test_a_malformed_stub_script_is_refused(tmp_path: Path):
    script = tmp_path / "script.yaml"
    script.write_text("- {unknown: 1}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        StubProvider(script)


def test_the_adapter_needs_a_key_or_a_script(llm_settings: Settings):
    with pytest.raises(LlmError):
        build_adapter(Settings(**{**llm_settings.__dict__, "llm_api_key": ""}))
    with pytest.raises(LlmError):
        build_adapter(Settings(**{**llm_settings.__dict__, "llm_provider": "stub"}))
