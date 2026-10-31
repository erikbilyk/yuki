"""Scripted-model turns on an in-process app: the stub provider plays the model from a script.

`Agent.turn(text, script)` writes the script as the YAML file WG_LLM_STUB_SCRIPT would name,
replays it through `agent_turn` for cus_main on the web channel, and keeps every message list
sent to the provider.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from itertools import count
from pathlib import Path
from typing import Any

import yaml

from wg.agent.llm import ModelAdapter, ModelReply, StubProvider, ToolCallRequest
from wg.agent.registry import Services, ToolContext, ToolRegistry
from wg.agent.turn import AgentTurn, agent_turn
from wg.contracts.enums import Channel, PlanMode
from wg.k6.db import Database

from ..api.conftest import Browser, make_client, make_settings


class RecordingStub(StubProvider):
    """The stub provider that also keeps every message list it was sent."""

    def __init__(self, script_path: Path) -> None:
        super().__init__(script_path)
        self.payloads: list[str] = []

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelReply:
        self.payloads.append(json.dumps(list(messages), ensure_ascii=False))
        return super().complete(messages, tools)


Results = list[dict[str, Any]]
Step = Callable[[Results], list[tuple[str, dict[str, Any]]]]


class ReadingModel:
    """A model that plays one reply per step; a step sees every tool result of the turn."""

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


class Agent:
    def __init__(self, browser: Browser, tmp_path: Path) -> None:
        self.browser = browser
        self.tmp_path = tmp_path
        self.db: Database = browser.db
        self.registry = ToolRegistry(self.db, Services.from_state(browser.client.app.state))
        self.log_path = tmp_path / "llm_calls.jsonl"
        self.stub: RecordingStub | None = None

    def turn(
        self,
        text: str,
        script: list[dict[str, Any]],
        history: Sequence[dict[str, Any]] = (),
    ) -> AgentTurn:
        path = self.tmp_path / "script.yaml"
        path.write_text(yaml.safe_dump(script, allow_unicode=True), encoding="utf-8")
        self.stub = RecordingStub(path)
        adapter = ModelAdapter(
            self.stub, make_settings(self.tmp_path / "wg.db"), log_path=self.log_path
        )
        return agent_turn(
            adapter,
            self.registry,
            customer_id="cus_main",
            channel=Channel.WEB,
            text=text,
            history=history,
        )

    def play(
        self, text: str, model: ReadingModel, mode: PlanMode = PlanMode.COORDINATED
    ) -> AgentTurn:
        """One turn with a model that reacts to the tool results it is sent."""
        adapter = ModelAdapter(
            model, make_settings(self.tmp_path / "wg.db"), log_path=self.log_path
        )
        return agent_turn(
            adapter,
            self.registry,
            customer_id="cus_main",
            channel=Channel.WEB,
            text=text,
            mode=mode,
        )

    def context(self, utterance: str = "") -> ToolContext:
        return ToolContext(customer_id="cus_main", channel=Channel.WEB, utterance=utterance)

    def rows(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def drafted(self, text: str, amount_fen: int, amount_quote: str) -> str:
        """A transfer to 妈妈 drafted by a correct model; returns its logical action id."""
        turn = self.turn(text, [draft(amount_fen, amount_quote), {"text": "好的。"}])
        return only_outcome(turn).result.logical_action_id

    def confirm(self, logical_action_id: str) -> dict[str, Any]:
        """Show the card and confirm it as the customer would, outside the model."""
        approval = self.registry.call(
            self.context(),
            "request_approval",
            {"intent": "transfer.create", "logical_action_id": logical_action_id},
        )
        card = self.browser.get(f"/v1/cards/{approval.result.card_id}").json()
        confirmed = self.browser.post(
            f"/v1/cards/{card['card_id']}/confirm", {"terms_hash": card["terms_hash"]}
        )
        assert confirmed.status_code in (201, 202), confirmed.text
        return confirmed.json()

    def transfers(self) -> list[dict[str, Any]]:
        return [
            json.loads(row["params_json"])
            for row in self.rows("SELECT params_json FROM actions WHERE kind = 'TRANSFER'")
        ]

    def database_text(self) -> str:
        with self.db.read() as conn:
            tables = [
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            ]
            return "\n".join(
                repr(tuple(row)) for t in tables for row in conn.execute(f'SELECT * FROM "{t}"')
            )


def draft(amount_fen: int, amount_quote: str, **extra: Any) -> dict[str, Any]:
    arguments = {
        "intent": "transfer.create",
        "payee_ref": "pay_mom",
        "payee_quote": "妈妈",
        "amount_fen": amount_fen,
        "amount_quote": amount_quote,
        **extra,
    }
    return {"tool_calls": [{"name": "draft_transfer", "arguments": arguments}]}


def only_outcome(result: AgentTurn):
    [outcome] = result.turn.outcomes
    return outcome


def running_agent(tmp_path: Path) -> Iterator[Agent]:
    """The agent for cus_main on the base profile, with the customer logged in for the card."""
    with make_client(tmp_path / "wg.db") as client:
        browser = Browser(client)
        browser.login("opr_demo")
        assert browser.sim("/sim/reset", {}).status_code == 200
        browser.login("cus_main")
        yield Agent(browser, tmp_path)


def running_s5_agent(tmp_path: Path) -> Iterator[Agent]:
    """The agent for cus_main on the direct-debit test profile (S5-SPEC §10)."""
    from wg.contracts.fixture import load_fixture
    from wg.k6 import fixture

    from ..s5_profile import write_profile

    with make_client(tmp_path / "wg.db") as client:
        browser = Browser(client)
        browser.login("opr_demo")
        fixture.reset(browser.db, load_fixture(write_profile(tmp_path)), None)
        browser.login("cus_main")
        yield Agent(browser, tmp_path)
