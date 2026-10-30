"""The Feishu adapter with a fake SDK sender: acknowledgement, duplicates, failures, contract."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from wg.im import feishu
from wg.im.feishu import (
    TOO_LONG_TEXT,
    UNAVAILABLE_TEXT,
    Adapter,
    AdapterFault,
    BackendUnavailable,
    HttpBackend,
    Incoming,
    Reply,
    SeenMessages,
    missing_settings,
    parse_event,
)
from wg.settings import load_settings

WAIT_S = 5.0


def event(
    message_id: str = "om_1",
    text: str | None = "你好",
    *,
    open_id: str = "ou_a",
    chat_id: str = "oc_1",
    chat_type: str = "p2p",
    message_type: str = "text",
    sender_type: str = "user",
) -> Any:
    """The attributes of the SDK's `P2ImMessageReceiveV1` object that the adapter reads."""
    content = json.dumps({"text": text}, ensure_ascii=False) if text is not None else "{}"
    data = SimpleNamespace(
        sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=open_id), sender_type=sender_type),
        message=SimpleNamespace(
            message_id=message_id,
            chat_id=chat_id,
            chat_type=chat_type,
            message_type=message_type,
            content=content,
        ),
    )
    return SimpleNamespace(event=data)


class FakeSender:
    """Records what would be sent to Feishu."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.arrived = threading.Condition()

    def send_text(self, chat_id: str, text: str, uuid: str) -> bool:
        with self.arrived:
            self.sent.append((chat_id, text, uuid))
            self.arrived.notify_all()
        return True

    def wait_for(self, count: int) -> None:
        with self.arrived:
            assert self.arrived.wait_for(lambda: len(self.sent) >= count, timeout=WAIT_S)


class FakeBackend:
    """Answers with a fixed reply, or raises; optionally waits for a gate before answering."""

    def __init__(
        self,
        text: str = "回复",
        *,
        duplicate: bool = False,
        error: Exception | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        self.text = text
        self.duplicate = duplicate
        self.error = error
        self.gate = gate
        self.calls: list[Incoming] = []

    def post_message(self, message: Incoming) -> Reply:
        self.calls.append(message)
        if self.gate is not None:
            assert self.gate.wait(WAIT_S)
        if self.error is not None:
            raise self.error
        return Reply(duplicate=self.duplicate, text=self.text)


def run(adapter: Adapter, *events: Any) -> None:
    for item in events:
        adapter.on_event(item)
    adapter.close()


def test_a_text_message_is_relayed_and_the_reply_goes_to_the_same_chat():
    backend, sender = FakeBackend("您好"), FakeSender()
    run(Adapter(backend, sender), event("om_7", "我的余额是多少？", chat_id="oc_9", open_id="ou_z"))
    assert backend.calls == [Incoming("om_7", "ou_z", "oc_9", "我的余额是多少？")]
    assert [(chat, text) for chat, text, _ in sender.sent] == [("oc_9", "您好")]


def test_the_reply_carries_a_stable_uuid_so_that_a_resend_is_not_shown_twice():
    first, second = FakeSender(), FakeSender()
    run(Adapter(FakeBackend(), first), event("om_1"))
    run(Adapter(FakeBackend(), second), event("om_1"))
    assert first.sent[0][2] == second.sent[0][2]
    assert len(first.sent[0][2]) <= 50


def test_a_redelivered_event_is_relayed_once():
    backend, sender = FakeBackend(), FakeSender()
    run(Adapter(backend, sender), event("om_1"), event("om_1"), event("om_1"))
    assert len(backend.calls) == 1
    assert len(sender.sent) == 1


def test_a_redelivery_while_the_first_is_still_running_is_dropped():
    gate = threading.Event()
    backend, sender = FakeBackend(gate=gate), FakeSender()
    adapter = Adapter(backend, sender)
    adapter.on_event(event("om_1"))
    adapter.on_event(event("om_1"))
    gate.set()
    adapter.close()
    assert len(backend.calls) == 1
    assert len(sender.sent) == 1


def test_a_backend_duplicate_sends_nothing():
    backend, sender = FakeBackend("", duplicate=True), FakeSender()
    run(Adapter(backend, sender), event("om_1"))
    assert len(backend.calls) == 1
    assert sender.sent == []


def test_a_slow_backend_does_not_delay_the_acknowledgement():
    gate = threading.Event()
    backend, sender = FakeBackend(gate=gate), FakeSender()
    adapter = Adapter(backend, sender)
    started = time.monotonic()
    adapter.on_event(event("om_1"))
    adapter.on_event(event("om_2", open_id="ou_b"))
    assert time.monotonic() - started < 0.5
    assert sender.sent == []
    gate.set()
    sender.wait_for(2)
    adapter.close()
    assert len(sender.sent) == 2


def test_one_users_messages_are_relayed_in_order():
    backend, sender = FakeBackend(), FakeSender()
    run(Adapter(backend, sender), *(event(f"om_{n}", f"m{n}") for n in range(20)))
    assert [call.text for call in backend.calls] == [f"m{n}" for n in range(20)]


def test_a_backend_error_gets_a_short_apology_and_the_next_message_still_works():
    sender = FakeSender()
    backend = FakeBackend(error=BackendUnavailable("status 500"))
    adapter = Adapter(backend, sender)
    adapter.on_event(event("om_1"))
    sender.wait_for(1)
    backend.error = None
    adapter.on_event(event("om_2"))
    adapter.close()
    assert [text for _, text, _ in sender.sent] == [UNAVAILABLE_TEXT, "回复"]


def test_a_refused_request_is_not_shown_to_the_user():
    backend, sender = FakeBackend(error=AdapterFault(403)), FakeSender()
    run(Adapter(backend, sender), event("om_1"))
    assert sender.sent == []


def test_an_unexpected_failure_does_not_stop_the_adapter():
    sender = FakeSender()
    backend = FakeBackend(error=RuntimeError("boom"))
    adapter = Adapter(backend, sender)
    adapter.on_event(event("om_1"))
    adapter.on_event(object())
    adapter.close()
    assert sender.sent == []


def test_a_long_message_is_answered_locally_without_the_backend():
    backend, sender = FakeBackend(), FakeSender()
    run(Adapter(backend, sender), event("om_1", "字" * 2001))
    assert backend.calls == []
    assert [text for _, text, _ in sender.sent] == [TOO_LONG_TEXT]


def test_a_message_at_the_limit_is_relayed():
    backend, sender = FakeBackend(), FakeSender()
    run(Adapter(backend, sender), event("om_1", "字" * 2000))
    assert len(backend.calls) == 1


def test_the_seen_set_forgets_the_oldest_id_first():
    seen = SeenMessages(capacity=2)
    assert [seen.first_time(i) for i in ("a", "b", "a", "c")] == [True, True, False, True]
    assert seen.first_time("a") is True


class TestParseEvent:
    def test_a_direct_text_message(self):
        assert parse_event(event("om_1", "你好")) == Incoming("om_1", "ou_a", "oc_1", "你好")

    def test_a_non_text_message_is_relayed_with_empty_text(self):
        message = parse_event(event(message_type="image", text=None))
        assert message is not None and message.text == ""

    @pytest.mark.parametrize("content", [None, "", "not json", "[]", '{"text": 5}', '{"x": 1}'])
    def test_unreadable_content_gives_empty_text(self, content):
        data = event()
        data.event.message.content = content
        message = parse_event(data)
        assert message is not None and message.text == ""

    def test_group_chats_and_bot_senders_are_ignored(self):
        assert parse_event(event(chat_type="group")) is None
        assert parse_event(event(sender_type="app")) is None

    @pytest.mark.parametrize("field", ["message_id", "chat_id"])
    def test_an_event_without_identifiers_is_ignored(self, field):
        data = event()
        setattr(data.event.message, field, None)
        assert parse_event(data) is None

    def test_an_event_without_a_sender_is_ignored(self):
        data = event()
        data.event.sender.sender_id.open_id = None
        assert parse_event(data) is None


def mock_backend(handler) -> HttpBackend:
    client = httpx.Client(base_url="http://backend.test", transport=httpx.MockTransport(handler))
    return HttpBackend("http://backend.test", "secret-value", client)


class TestHttpBackend:
    def test_the_request_follows_the_route_contract(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            body = {
                "message_id": "om_1",
                "duplicate": False,
                "bound": True,
                "text": "好",
                "links": [],
            }
            return httpx.Response(200, json=body)

        reply = mock_backend(handler).post_message(Incoming("om_1", "ou_a", "oc_1", "你好"))
        assert reply == Reply(duplicate=False, text="好")
        request = seen[0]
        assert (request.method, request.url.path) == ("POST", "/v1/channels/feishu/messages")
        assert request.headers["X-Channel-Secret"] == "secret-value"
        assert json.loads(request.content) == {
            "message_id": "om_1",
            "open_id": "ou_a",
            "chat_id": "oc_1",
            "text": "你好",
        }

    def test_the_default_client_ignores_proxy_variables(self, monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        backend = HttpBackend("http://127.0.0.1:8100", "s")
        assert backend._client._trust_env is False
        assert backend._client.timeout.read >= 60
        backend.close()

    @pytest.mark.parametrize("status", [403, 422])
    def test_adapter_faults(self, status):
        backend = mock_backend(lambda request: httpx.Response(status, json={"code": "X"}))
        with pytest.raises(AdapterFault) as caught:
            backend.post_message(Incoming("om_1", "ou_a", "oc_1", "x"))
        assert caught.value.status == status

    @pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(502, text="bad")])
    def test_a_server_error_is_unavailable(self, response):
        backend = mock_backend(lambda request: response)
        with pytest.raises(BackendUnavailable):
            backend.post_message(Incoming("om_1", "ou_a", "oc_1", "x"))

    @pytest.mark.parametrize("body", [b"not json", b"{}", b"[]"])
    def test_a_malformed_reply_is_unavailable(self, body):
        backend = mock_backend(lambda request: httpx.Response(200, content=body))
        with pytest.raises(BackendUnavailable):
            backend.post_message(Incoming("om_1", "ou_a", "oc_1", "x"))

    @pytest.mark.parametrize("error", [httpx.ConnectError("no"), httpx.ReadTimeout("slow")])
    def test_a_network_failure_is_unavailable_and_hides_the_message(self, error):
        def handler(request: httpx.Request) -> httpx.Response:
            raise error

        with pytest.raises(BackendUnavailable) as caught:
            mock_backend(handler).post_message(Incoming("om_1", "ou_a", "oc_1", "机密正文"))
        assert "机密正文" not in str(caught.value)


class TestSettings:
    def test_all_three_names_are_required(self, tmp_path):
        settings = load_settings(env={}, env_file=tmp_path / "none.env")
        assert missing_settings(settings) == [
            "WG_FEISHU_APP_ID",
            "WG_FEISHU_APP_SECRET",
            "WG_CHANNEL_SECRET",
        ]

    def test_set_values_are_not_reported(self, tmp_path):
        env = {"WG_FEISHU_APP_ID": "a", "WG_FEISHU_APP_SECRET": "b", "WG_CHANNEL_SECRET": "c"}
        assert missing_settings(load_settings(env=env, env_file=tmp_path / "none.env")) == []

    def test_main_stops_without_credentials(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            feishu, "load_settings", lambda env=None: load_settings(env={}, env_file=tmp_path / "x")
        )
        assert feishu.main() == 2


def test_nothing_the_adapter_logs_holds_message_text_or_secrets(caplog):
    caplog.set_level("DEBUG", logger="wg.feishu")
    secret_text = "密码是 hunter2 机密正文"
    cases = [
        FakeBackend(secret_text),
        FakeBackend(error=BackendUnavailable("status 500")),
        FakeBackend(error=AdapterFault(403)),
        FakeBackend(error=RuntimeError(secret_text)),
    ]
    for number, backend in enumerate(cases):
        run(Adapter(backend, FakeSender()), event(f"om_{number}", secret_text))
    assert "hunter2" not in caplog.text
    assert "机密正文" not in caplog.text
    assert "ou_a" not in caplog.text
