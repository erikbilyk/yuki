"""An in-process app, a tool registry on it and a model provider the tests script."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wg.agent.llm import ModelAdapter, ModelReply, ToolCallRequest, Usage
from wg.agent.registry import Services, ToolContext, ToolRegistry
from wg.contracts.enums import Channel
from wg.settings import Settings

from ..api.conftest import make_client, make_settings
from .scripted import Agent, running_agent, running_s5_agent


class ScriptedProvider:
    """Replies from a list of `ModelReply`; records the tools offered with each call."""

    name = "scripted"

    def __init__(self, replies: Sequence[ModelReply] = ()) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelReply:
        self.calls.append((list(messages), list(tools)))
        return self.replies.pop(0)


def tool_call(name: str, arguments: dict[str, Any], index: int = 0) -> ToolCallRequest:
    import json

    return ToolCallRequest(f"call_{index}", name, json.dumps(arguments), arguments)


def reply(*calls: ToolCallRequest, text: str = "", usage: Usage | None = None) -> ModelReply:
    return ModelReply(text=text, tool_calls=calls, usage=usage or Usage(), returned_model="test")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path / "wg.db") as test_client:
        yield test_client


@pytest.fixture
def registry(client: TestClient) -> ToolRegistry:
    state = client.app.state
    return ToolRegistry(state.db, Services.from_state(state))


@pytest.fixture
def context() -> ToolContext:
    return ToolContext(customer_id="cus_main", channel=Channel.WEB, utterance="给妈妈转500元")


@pytest.fixture
def llm_settings(tmp_path: Path) -> Settings:
    base = make_settings(tmp_path / "wg.db")
    return Settings(
        **{
            **base.__dict__,
            "llm_provider": "deepseek",
            "llm_api_key": "sk-test-key-0123456789",
            "llm_base_url": "https://llm.test",
            "llm_spend_cap_cny": 1,
        }
    )


@pytest.fixture
def adapter_factory(llm_settings: Settings, tmp_path: Path):
    def build(provider: ScriptedProvider, settings: Settings | None = None) -> ModelAdapter:
        return ModelAdapter(
            provider, settings or llm_settings, log_path=tmp_path / "llm_calls.jsonl"
        )

    return build


@pytest.fixture
def agent(tmp_path: Path) -> Iterator[Agent]:
    """Scripted turns for cus_main on the base profile (`scripted.Agent`)."""
    yield from running_agent(tmp_path)


@pytest.fixture
def s5(tmp_path: Path) -> Iterator[Agent]:
    """Scripted turns for cus_main on the direct-debit test profile."""
    yield from running_s5_agent(tmp_path)
