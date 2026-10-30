"""Chat routes: one agent turn per message, masked storage and history (SPEC §18.4, §18.6)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from wg.agent.chat import ChatService
from wg.agent.llm import ModelAdapter
from wg.agent.registry import Services, ToolRegistry
from wg.contracts.api import ChatHistoryView, ChatReply
from wg.contracts.enums import Disposition, Role
from wg.contracts.errors import ErrorBody, ErrorCode

from ..agent.scripted import RecordingStub
from .conftest import Browser, make_settings

pytestmark = pytest.mark.layer("U")

PIN = "731946"
DRAFT_500 = {
    "tool_calls": [
        {
            "name": "draft_transfer",
            "arguments": {
                "intent": "transfer.create",
                "payee_ref": "pay_mom",
                "payee_quote": "妈妈",
                "amount_fen": 50_000,
                "amount_quote": "500",
            },
        }
    ]
}


def scripted(browser: Browser, tmp_path: Path, script: list[dict[str, Any]]) -> RecordingStub:
    """Serve the chat route from a stub model that plays `script` and records its payloads."""
    path = tmp_path / "script.yaml"
    path.write_text(yaml.safe_dump(script, allow_unicode=True), encoding="utf-8")
    stub = RecordingStub(path)
    settings = make_settings(tmp_path / "wg.db")
    state = browser.client.app.state
    state.chat = ChatService(
        ToolRegistry(state.db, Services.from_state(state)),
        lambda: ModelAdapter(stub, settings, log_path=tmp_path / "llm_calls.jsonl"),
    )
    return stub


def send(browser: Browser, text: str) -> ChatReply:
    response = browser.post("/v1/chat/messages", {"text": text})
    assert response.status_code == 200, response.text
    return ChatReply.model_validate(response.json())


def history(browser: Browser) -> ChatHistoryView:
    response = browser.get("/v1/chat/history")
    assert response.status_code == 200, response.text
    return ChatHistoryView.model_validate(response.json())


def rows(browser: Browser, sql: str, *params):
    with browser.db.read() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def test_a_turn_drafts_and_stores_only_the_masked_text(browser: Browser, tmp_path: Path):
    scripted(browser, tmp_path, [DRAFT_500, {"text": "已为您准备好给妈妈转账500元的草稿。"}])
    reply = send(browser, f"给妈妈转500，我的密码{PIN}")

    [transfer] = rows(browser, "SELECT logical_action_id, params_json FROM actions")
    assert reply.logical_action_ids == (transfer["logical_action_id"],)
    assert '"amount_fen":50000' in transfer["params_json"]
    assert reply.explanation == "已为您准备好给妈妈转账500元的草稿。"
    assert not reply.explanation_replaced and reply.fact_lines

    stored = rows(browser, "SELECT role, channel, masked_text, k7_replaced FROM chat_messages")
    assert [(r["role"], r["channel"], r["k7_replaced"]) for r in stored] == [
        ("user", "web", 0),
        ("assistant", "web", 0),
    ]
    assert PIN not in stored[0]["masked_text"] and "[已隐藏]" in stored[0]["masked_text"]
    assert stored[1]["masked_text"].endswith(reply.explanation)
    with browser.db.read() as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        dump = "".join(repr(tuple(r)) for t in tables for r in conn.execute(f'SELECT * FROM "{t}"'))
    assert PIN not in dump

    view = history(browser)
    assert [m.role.value for m in view.messages] == ["user", "assistant"]
    assert view.messages[0].text == stored[0]["masked_text"]


def test_the_session_history_goes_to_the_model_with_the_next_turn(browser: Browser, tmp_path: Path):
    stub = scripted(browser, tmp_path, [{"text": "您好，请问需要什么帮助？"}, {"text": "好的。"}])
    send(browser, "你好")
    send(browser, "谢谢")
    second = stub.payloads[1]
    assert "你好" in second and "您好，请问需要什么帮助？" in second and "谢谢" in second


def test_a_replaced_explanation_is_stored_as_replaced(browser: Browser, tmp_path: Path):
    scripted(browser, tmp_path, [DRAFT_500, {"text": "已转账给妈妈500元，已到账。"}])
    reply = send(browser, "给妈妈转500")
    assert reply.explanation_replaced and "已到账" not in (reply.explanation or "")
    [answer] = rows(browser, "SELECT k7_replaced FROM chat_messages WHERE role = 'assistant'")
    assert answer == {"k7_replaced": 1}
    assert history(browser).messages[-1].explanation_replaced


def test_without_a_model_the_route_answers_503_and_stores_nothing(browser: Browser):
    # The test app has the stub provider without a script, so no model can be built.
    response = browser.post("/v1/chat/messages", {"text": f"余额多少？密码{PIN}"})
    assert response.status_code == 503
    body = ErrorBody.model_validate(response.json())
    assert body.code is ErrorCode.PROVIDER_TIMEOUT
    assert body.detail == {"reason": "stub_script_missing"}
    assert rows(browser, "SELECT role FROM chat_messages") == []


def test_a_failed_model_call_after_ingress_keeps_the_user_message(browser: Browser, tmp_path: Path):
    scripted(browser, tmp_path, [])
    response = browser.post("/v1/chat/messages", {"text": f"余额多少？密码{PIN}"})
    assert response.status_code == 503
    assert ErrorBody.model_validate(response.json()).detail == {"reason": "stub_exhausted"}
    [stored] = rows(browser, "SELECT role, masked_text FROM chat_messages")
    assert stored["role"] == "user" and PIN not in stored["masked_text"]


def test_compose_is_refused_after_the_demo_sets_flat_mode(browser: Browser, tmp_path: Path):
    compose = {
        "tool_calls": [
            {
                "name": "compose_plan",
                "arguments": {"intent": "plan.manage", "draft_ids": ["act_x"], "depends_on": {}},
            }
        ]
    }
    scripted(browser, tmp_path, [compose, {"text": "暂不支持组合计划。"}])
    assert browser.sim("/sim/agent-mode", {"mode": "flat"}).status_code == 204
    reply = send(browser, "把这些合成一个计划")
    assert reply.disposition is Disposition.UNSUPPORTED
    refusal = rows(browser, "SELECT ref, decision FROM audit_log WHERE event = 'tool_refused'")
    assert refusal == [{"ref": "compose_plan", "decision": "UNSUPPORTED_REQUEST"}]


def test_chat_needs_csrf_and_history_is_the_customers_own(browser: Browser, tmp_path: Path):
    scripted(browser, tmp_path, [{"text": "您好。"}])
    without = browser.post("/v1/chat/messages", {"text": "你好"}, csrf=False)
    assert without.status_code == 403
    send(browser, "你好")
    assert len(history(browser).messages) == 2

    browser.login("cus_other")
    assert history(browser).messages == ()
    assert browser.post("/v1/chat/messages", {"text": ""}, role=Role.CUSTOMER).status_code == 422
