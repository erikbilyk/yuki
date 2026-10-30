"""The adapter against a real backend process: unbound user, duplicate delivery, wrong secret."""

from __future__ import annotations

from tests.b.harness.server import BackendServer
from wg.im.feishu import UNAVAILABLE_TEXT, Adapter, HttpBackend

from .test_feishu_adapter import FakeSender, event


def adapter_for(server: BackendServer, sender: FakeSender, secret: str | None = None) -> Adapter:
    secret = server.env["WG_CHANNEL_SECRET"] if secret is None else secret
    return Adapter(HttpBackend(server.base_url, secret), sender)


def test_an_unbound_user_gets_the_instruction_and_a_redelivery_gets_nothing(server: BackendServer):
    sender = FakeSender()
    adapter = adapter_for(server, sender)
    adapter.on_event(event("om_1", "你好", open_id="ou_stranger", chat_id="oc_7"))
    adapter.on_event(event("om_1", "你好", open_id="ou_stranger", chat_id="oc_7"))
    adapter.close()
    assert [chat for chat, _, _ in sender.sent] == ["oc_7"]
    assert "绑定" in sender.sent[0][1]


def test_a_message_the_backend_has_seen_is_not_answered_twice(server: BackendServer):
    first, second = FakeSender(), FakeSender()
    for sender in (first, second):
        adapter = adapter_for(server, sender)
        adapter.on_event(event("om_9", "你好", open_id="ou_stranger"))
        adapter.close()
    assert len(first.sent) == 1
    assert second.sent == []


def test_a_wrong_channel_secret_sends_nothing_to_the_user(server: BackendServer):
    sender = FakeSender()
    adapter = adapter_for(server, sender, secret="wrong")
    adapter.on_event(event("om_1", "你好"))
    adapter.close()
    assert sender.sent == []


def test_a_backend_that_is_down_gets_a_short_apology(server: BackendServer):
    adapter = adapter_for(server, sender := FakeSender())
    server.stop()
    adapter.on_event(event("om_1", "你好"))
    adapter.close()
    assert [text for _, text, _ in sender.sent] == [UNAVAILABLE_TEXT]
