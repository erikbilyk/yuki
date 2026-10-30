"""The IM channel: pairing, binding, duplicate delivery, unbound users and refused approvals."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from wg.agent.chat import ChatService
from wg.agent.llm import ModelAdapter
from wg.agent.registry import Services, ToolRegistry
from wg.contracts.api import Card, ChannelReply, ConfirmResult, PairingCodeView
from wg.im import service as im_text
from wg.im.service import ImService, is_approval_attempt

from ..agent.scripted import RecordingStub
from ..core.drafts import MAIN, RED_FEN, YELLOW_FEN, transfer_card
from .conftest import Browser, make_client, make_settings

pytestmark = pytest.mark.layer("U")

SECRET = {"X-Channel-Secret": "test-channel-secret"}
BASE = "http://127.0.0.1:8100"


def rows(browser: Browser, sql: str, *params):
    with browser.db.read() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def pairing_code(browser: Browser) -> PairingCodeView:
    response = browser.post("/v1/channels/pairing-code")
    assert response.status_code == 201, response.text
    return PairingCodeView.model_validate(response.json())


def message(
    browser: Browser, text: str, *, message_id: str, open_id: str = "ou_alice", headers=SECRET
):
    body = {"message_id": message_id, "open_id": open_id, "chat_id": "oc_chat", "text": text}
    return browser.client.post("/v1/channels/feishu/messages", json=body, headers=headers)


def say(browser: Browser, text: str, *, message_id: str, open_id: str = "ou_alice") -> ChannelReply:
    response = message(browser, text, message_id=message_id, open_id=open_id)
    assert response.status_code == 200, response.text
    return ChannelReply.model_validate(response.json())


def bind(browser: Browser, open_id: str = "ou_alice") -> ChannelReply:
    code = pairing_code(browser).code
    reply = say(browser, f"绑定 {code}", message_id=f"bind-{open_id}-{code}", open_id=open_id)
    assert reply.bound and reply.text == im_text.BOUND_TEXT
    return reply


def scripted(browser: Browser, tmp_path: Path, script: list[dict[str, Any]]) -> RecordingStub:
    """Serve the IM route from a stub model that plays `script` and records its payloads."""
    path = tmp_path / "script.yaml"
    path.write_text(yaml.safe_dump(script, allow_unicode=True), encoding="utf-8")
    stub = RecordingStub(path)
    settings = make_settings(tmp_path / "wg.db")
    state = browser.client.app.state
    state.chat = ChatService(
        ToolRegistry(state.db, Services.from_state(state)),
        lambda: ModelAdapter(stub, settings, log_path=tmp_path / "llm_calls.jsonl"),
    )
    state.im = ImService(state.db, state.chat, code_key=settings.code_hmac_key, web_base_url=BASE)
    return stub


def test_a_pairing_code_binds_the_user_once(browser: Browser):
    view = pairing_code(browser)
    stored = rows(browser, "SELECT code_hmac, customer_id, consumed_at FROM im_pairing_codes")
    assert len(stored) == 1 and stored[0]["customer_id"] == MAIN
    assert view.code not in stored[0]["code_hmac"] and stored[0]["consumed_at"] is None

    reply = say(browser, f"绑定 {view.code}", message_id="m1")
    assert reply.bound and not reply.duplicate and reply.text == im_text.BOUND_TEXT
    [binding] = rows(browser, "SELECT * FROM im_bindings")
    assert (binding["channel"], binding["external_user_id"], binding["customer_id"]) == (
        "feishu",
        "ou_alice",
        MAIN,
    )
    assert binding["revoked_at"] is None
    assert rows(browser, "SELECT consumed_at FROM im_pairing_codes")[0]["consumed_at"]

    again = say(browser, f"绑定 {view.code}", message_id="m2", open_id="ou_bob")
    assert not again.bound and again.text == im_text.BIND_FAILED_TEXT
    assert [r["external_user_id"] for r in rows(browser, "SELECT * FROM im_bindings")] == [
        "ou_alice"
    ]
    events = rows(browser, "SELECT event, decision FROM audit_log WHERE event LIKE 'im_%'")
    assert events == [
        {"event": "im_bound", "decision": None},
        {"event": "im_bind_refused", "decision": "used_or_expired"},
    ]


def test_the_pairing_code_is_five_simulated_minutes(browser: Browser):
    view = pairing_code(browser)
    clock = browser.sim("/sim/clock/advance", {"minutes": 0}).json()
    assert view.expires_at - datetime.fromisoformat(clock["now"]) == timedelta(minutes=5)

    assert browser.sim("/sim/clock/advance", {"minutes": 5}).status_code == 200
    reply = say(browser, f"绑定 {view.code}", message_id="late")
    assert not reply.bound and reply.text == im_text.BIND_FAILED_TEXT
    assert rows(browser, "SELECT * FROM im_bindings") == []

    fresh = pairing_code(browser)
    assert browser.sim("/sim/clock/advance", {"minutes": 4}).status_code == 200
    assert say(browser, f"绑定 {fresh.code}", message_id="in-time").bound


def test_a_new_code_replaces_the_old_one_and_an_unknown_code_binds_nothing(browser: Browser):
    old = pairing_code(browser)
    new = pairing_code(browser)
    assert not say(browser, f"绑定 {old.code}", message_id="m1").bound
    wrong = f"{(int(new.code) + 1) % 10**6:06d}"
    assert not say(browser, f"绑定 {wrong}", message_id="m2").bound
    assert say(browser, f"绑定 {new.code}", message_id="m3").bound
    assert [
        r["decision"]
        for r in rows(browser, "SELECT decision FROM audit_log WHERE event = 'im_bind_refused'")
    ] == [
        "used_or_expired",
        "unknown_code",
    ]


def test_binding_a_user_to_another_customer_moves_the_binding(tmp_path: Path):
    with make_client(tmp_path / "wg.db") as client:
        browser = Browser(client)
        browser.login("opr_demo")
        assert browser.sim("/sim/reset", {}).status_code == 200
        browser.login("cus_main")
        bind(browser)
        browser.login("cus_other")
        bind(browser)
        stored = rows(
            browser, "SELECT customer_id, revoked_at FROM im_bindings ORDER BY binding_id"
        )
        assert [(r["customer_id"], r["revoked_at"] is None) for r in stored] == [
            (MAIN, False),
            ("cus_other", True),
        ]


def test_pairing_code_is_for_a_customer_session(browser: Browser):
    assert browser.post("/v1/channels/pairing-code", csrf=False).status_code == 403
    browser.client.cookies.clear()
    assert browser.post("/v1/channels/pairing-code").status_code == 401


@pytest.mark.parametrize("headers", [{}, {"X-Channel-Secret": "wrong"}, {"X-Channel-Secret": ""}])
def test_the_message_route_needs_the_channel_secret(browser: Browser, headers):
    response = message(browser, "你好", message_id="m1", headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_ROUTE"
    assert rows(browser, "SELECT * FROM im_messages_seen") == []


def test_the_message_route_needs_no_session_or_csrf(browser: Browser):
    browser.client.cookies.clear()
    assert say(browser, "你好", message_id="m1").text == im_text.UNBOUND_TEXT


def test_an_unbound_user_gets_instructions_and_no_data(browser: Browser, tmp_path: Path):
    stub = scripted(browser, tmp_path, [{"text": "不应被调用"}])
    reply = say(browser, "我的余额是多少", message_id="m1")
    assert not reply.bound and not reply.duplicate and reply.links == ()
    assert reply.text == im_text.UNBOUND_TEXT and "¥" not in reply.text
    assert stub.payloads == []
    assert rows(browser, "SELECT * FROM chat_messages") == []
    assert rows(browser, "SELECT event, customer_id FROM audit_log WHERE event LIKE 'im_%'") == [
        {"event": "im_unbound_message", "customer_id": None}
    ]


def test_a_redelivered_message_is_ignored(browser: Browser, tmp_path: Path):
    stub = scripted(browser, tmp_path, [{"text": "您好，请问需要什么帮助？"}, {"text": "不应出现"}])
    bind(browser)
    first = say(browser, "你好", message_id="om_1")
    assert first.bound and first.text and not first.duplicate
    second = say(browser, "你好", message_id="om_1")
    assert second.duplicate and second.bound and second.text == "" and second.links == ()
    assert len(stub.payloads) == 1
    assert [r["role"] for r in rows(browser, "SELECT role FROM chat_messages")] == [
        "user",
        "assistant",
    ]
    assert len(rows(browser, "SELECT * FROM im_messages_seen WHERE message_id = 'om_1'")) == 1


def test_a_bound_user_goes_through_the_agent_on_the_im_channel(browser: Browser, tmp_path: Path):
    scripted(browser, tmp_path, [{"text": "您好，请问需要什么帮助？"}])
    bind(browser)
    reply = say(browser, "你好", message_id="om_1")
    assert reply.bound and reply.text.endswith("您好，请问需要什么帮助？") and reply.links == ()
    stored = rows(browser, "SELECT session_id, customer_id, channel, role FROM chat_messages")
    assert {(r["session_id"], r["customer_id"], r["channel"]) for r in stored} == {
        ("feishu:ou_alice", MAIN, "im")
    }
    web = browser.get("/v1/chat/history").json()["messages"]
    assert [m["channel"] for m in web] == ["im", "im"]


def test_a_draft_that_needs_approval_comes_back_with_the_card_link(
    browser: Browser, tmp_path: Path
):
    action, card = transfer_card(browser.db, RED_FEN, customer_id=MAIN)
    request = {
        "tool_calls": [
            {
                "name": "request_approval",
                "arguments": {
                    "intent": "transfer.create",
                    "logical_action_id": action.logical_action_id,
                },
            }
        ]
    }
    scripted(browser, tmp_path, [request, {"text": "请核对卡片后确认。"}])
    bind(browser)
    reply = say(browser, "请把这笔转账的卡片给我", message_id="om_1")
    assert reply.links == (f"{BASE}/cards/{card.card_id}",)
    assert reply.text.endswith(f"{im_text.NEEDS_CARD_TEXT}\n{reply.links[0]}")
    assert rows(browser, "SELECT * FROM approvals") == []


def test_a_model_failure_gets_a_short_apology(browser: Browser, tmp_path: Path):
    scripted(browser, tmp_path, [])
    bind(browser)
    reply = say(browser, "你好", message_id="om_1")
    assert reply.bound and reply.text == im_text.UNAVAILABLE_TEXT


def test_an_approval_attempt_is_refused_with_the_card_link(browser: Browser, tmp_path: Path):
    stub = scripted(browser, tmp_path, [{"text": "不应被调用"}])
    _, card = transfer_card(browser.db, YELLOW_FEN, customer_id=MAIN)
    bind(browser)
    reply = say(browser, "确认转账", message_id="om_1")
    assert reply.bound and reply.links == (f"{BASE}/cards/{card.card_id}",)
    assert reply.text.startswith(im_text.APPROVAL_REFUSED_TEXT)
    assert reply.text.endswith(reply.links[0])
    assert stub.payloads == []
    assert rows(browser, "SELECT * FROM approvals") == []
    assert rows(browser, "SELECT * FROM chat_messages") == []
    assert rows(
        browser, "SELECT actor, decision FROM audit_log WHERE event = 'im_approval_refused'"
    ) == [{"actor": "customer", "decision": "FORBIDDEN_ROUTE"}]
    shown = Card.model_validate(browser.get(f"/v1/cards/{card.card_id}").json())
    assert shown.approval_id is None


def test_an_approval_attempt_without_a_card_points_to_the_web_app(browser: Browser, tmp_path: Path):
    scripted(browser, tmp_path, [])
    bind(browser)
    reply = say(browser, "我同意", message_id="om_1")
    assert reply.links == (BASE,) and reply.text.endswith(f"{im_text.HOME_LINK_TEXT}{BASE}")


def test_a_code_sent_in_im_is_refused_and_never_stored(browser: Browser, tmp_path: Path):
    stub = scripted(browser, tmp_path, [{"text": "不应被调用"}])
    _, card = transfer_card(browser.db, RED_FEN, customer_id=MAIN)
    terms_hash = browser.get(f"/v1/cards/{card.card_id}").json()["terms_hash"]
    confirmed = browser.post(f"/v1/cards/{card.card_id}/confirm", {"terms_hash": terms_hash})
    result = ConfirmResult.model_validate(confirmed.json())
    assert result.challenge is not None
    code = browser.get("/v1/sms").json()["messages"][0]["code"]
    bind(browser)

    for number, text in enumerate([code, f"验证码 {code}", f"the code is {code}"]):
        reply = say(browser, text, message_id=f"om_{number}")
        assert reply.text.startswith(im_text.APPROVAL_REFUSED_TEXT) and len(reply.links) == 1
        assert code not in reply.text
    assert stub.payloads == []
    with browser.db.read() as conn:
        audit_text = repr([tuple(r) for r in conn.execute("SELECT * FROM audit_log")])
        chat_text = repr([tuple(r) for r in conn.execute("SELECT * FROM chat_messages")])
    assert code not in audit_text + chat_text
    assert rows(browser, "SELECT verified_at FROM challenges") == [{"verified_at": None}]


def test_secrets_in_an_im_request_are_masked_like_in_web_chat(browser: Browser, tmp_path: Path):
    stub = scripted(browser, tmp_path, [{"text": "好的。"}])
    bind(browser)
    say(browser, "余额多少？密码731946", message_id="om_1")
    assert "731946" not in stub.payloads[0]
    [stored] = rows(browser, "SELECT masked_text FROM chat_messages WHERE role = 'user'")
    assert "731946" not in stored["masked_text"] and "[已隐藏]" in stored["masked_text"]


def test_a_blank_message_is_not_sent_to_the_model(browser: Browser, tmp_path: Path):
    stub = scripted(browser, tmp_path, [])
    bind(browser)
    assert say(browser, "  ", message_id="om_1").text == im_text.BLANK_TEXT
    assert stub.payloads == []


@pytest.mark.parametrize(
    "text",
    [
        "确认",
        "确认转账",
        "请确认这笔转账",
        "我同意",
        "批准吧",
        "确认无误，执行",
        "好的，确认",
        "我确认给妈妈转500",
        "同意付款",
        "approve",
        "Please confirm the transfer",
        "OK",
        "yes",
    ],
)
def test_approval_attempts_are_recognised(text: str):
    assert is_approval_attempt(text)


@pytest.mark.parametrize(
    "text",
    [
        "给妈妈转500元",
        "确认一下我的余额",
        "帮我确认转账记录",
        "我的账单有哪些",
        "这笔转账的状态是什么",
        "unconfirmed balance",
        "取消刚才的转账",
    ],
)
def test_requests_are_not_taken_for_approvals(text: str):
    assert not is_approval_attempt(text)
