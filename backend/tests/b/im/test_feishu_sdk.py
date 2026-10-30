"""The adapter against the installed lark-oapi: real event objects and real request builders."""

from __future__ import annotations

import json
from typing import Any

import pytest

from wg.im.feishu import Adapter, Incoming, LarkSender, Reply, build_stream_client
from wg.settings import load_settings

pytest.importorskip("lark_oapi")

PAYLOAD = {
    "schema": "2.0",
    "header": {
        "event_id": "ev_1",
        "event_type": "im.message.receive_v1",
        "create_time": "1700000000000",
        "token": "",
        "app_id": "cli_test",
        "tenant_key": "tenant",
    },
    "event": {
        "sender": {
            "sender_id": {"open_id": "ou_alice", "user_id": "u1", "union_id": "on_1"},
            "sender_type": "user",
            "tenant_key": "tenant",
        },
        "message": {
            "message_id": "om_100",
            "chat_id": "oc_200",
            "chat_type": "p2p",
            "message_type": "text",
            "content": json.dumps({"text": "我的余额是多少？"}, ensure_ascii=False),
            "create_time": "1700000000000",
        },
    },
}


class Recorder:
    def __init__(self) -> None:
        self.calls: list[Incoming] = []
        self.sent: list[tuple[str, str, str]] = []

    def post_message(self, message: Incoming) -> Reply:
        self.calls.append(message)
        return Reply(duplicate=False, text="余额")

    def send_text(self, chat_id: str, text: str, uuid: str) -> bool:
        self.sent.append((chat_id, text, uuid))
        return True


def settings(tmp_path):
    env = {
        "WG_FEISHU_APP_ID": "cli_test",
        "WG_FEISHU_APP_SECRET": "not-a-real-secret",
        "WG_CHANNEL_SECRET": "c",
    }
    return load_settings(env=env, env_file=tmp_path / "none.env")


def test_the_registered_handler_receives_a_real_event_through_the_sdk_dispatcher(tmp_path):
    recorder = Recorder()
    adapter = Adapter(recorder, recorder)
    client = build_stream_client(settings(tmp_path), adapter)
    # The long-connection client passes every event frame to this method.
    result = client._event_handler._do_without_validation(json.dumps(PAYLOAD).encode())
    adapter.close()
    assert result is None
    assert recorder.calls == [Incoming("om_100", "ou_alice", "oc_200", "我的余额是多少？")]
    assert recorder.sent[0][:2] == ("oc_200", "余额")


def test_the_stream_client_is_created_without_a_network_call(tmp_path):
    import lark_oapi as lark

    client = build_stream_client(settings(tmp_path), Adapter(Recorder(), Recorder()))
    assert isinstance(client, lark.ws.Client)


class FakeResponse:
    def __init__(self, code: int) -> None:
        self.code = code

    def success(self) -> bool:
        return self.code == 0


class FakeLarkClient:
    """Stands in for `lark.Client`: records `im.v1.message.create` requests."""

    def __init__(self, *outcomes: int | Exception) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[Any] = []
        self.im = self
        self.v1 = self
        self.message = self

    def create(self, request: Any) -> FakeResponse:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


def test_the_send_request_addresses_the_chat_as_the_bot():
    client = FakeLarkClient(0)
    assert LarkSender(client).send_text("oc_200", '好的 "引号"', "abc") is True
    request = client.requests[0]
    assert request.receive_id_type == "chat_id"
    body = request.request_body
    assert (body.receive_id, body.msg_type, body.uuid) == ("oc_200", "text", "abc")
    assert json.loads(body.content) == {"text": '好的 "引号"'}


def test_a_failed_send_is_tried_once_more_with_the_same_uuid():
    client = FakeLarkClient(99991400, 0)
    assert LarkSender(client).send_text("oc_200", "好", "abc") is True
    assert [r.request_body.uuid for r in client.requests] == ["abc", "abc"]


@pytest.mark.parametrize("outcomes", [(1, 1), (ConnectionError("x"), 1), (1, TimeoutError("x"))])
def test_a_send_that_keeps_failing_is_reported_not_raised(outcomes):
    client = FakeLarkClient(*outcomes)
    assert LarkSender(client).send_text("oc_200", "好", "abc") is False
    assert len(client.requests) == 2
