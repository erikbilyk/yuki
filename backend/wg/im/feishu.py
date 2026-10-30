"""Feishu channel adapter: a long-connection bot that relays direct messages to the backend.

Runs as its own process (`python -m wg.im.feishu`, extra `feishu`). Each `im.message.receive_v1`
event is acknowledged at once, because Feishu re-delivers an event that is not handled within
3 seconds, and the message is relayed to `POST /v1/channels/feishu/messages` on a worker thread.
The backend decides everything; the adapter only transports text and the reply (SPEC §21).
Message text and credentials are never logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import threading
import zlib
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from wg.settings import Settings, load_settings

log = logging.getLogger("wg.feishu")

MESSAGE_ROUTE = "/v1/channels/feishu/messages"
CHANNEL_HEADER = "X-Channel-Secret"
MAX_TEXT_CHARS = 2000
SEEN_CAPACITY = 4096
WORKERS = 8
# The agent turn calls the model; the backend answers within its own model timeout.
BACKEND_TIMEOUT_S = 75.0
CONNECT_TIMEOUT_S = 5.0
SEND_ATTEMPTS = 2

UNAVAILABLE_TEXT = "服务暂时不可用，请稍后再试。"
TOO_LONG_TEXT = "消息太长，请缩短后重新发送。"


@dataclass(frozen=True)
class Incoming:
    """One direct text message as the backend route expects it."""

    message_id: str
    open_id: str
    chat_id: str
    text: str


@dataclass(frozen=True)
class Reply:
    """The part of the backend answer the adapter acts on."""

    duplicate: bool
    text: str


class BackendUnavailable(Exception):
    """The backend did not give a usable answer; the outcome of the message is unknown."""


class AdapterFault(Exception):
    """The backend refused the request (wrong secret or malformed body); not a customer matter."""

    def __init__(self, status: int) -> None:
        super().__init__(f"backend refused the request with status {status}")
        self.status = status


class Backend(Protocol):
    def post_message(self, message: Incoming) -> Reply: ...


class Sender(Protocol):
    def send_text(self, chat_id: str, text: str, uuid: str) -> bool: ...


class HttpBackend:
    """Calls the backend message route; never reads proxy variables (127.0.0.1 is direct)."""

    def __init__(self, base_url: str, channel_secret: str, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            base_url=base_url,
            trust_env=False,
            timeout=httpx.Timeout(BACKEND_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        )
        self._secret = channel_secret

    def post_message(self, message: Incoming) -> Reply:
        body = {
            "message_id": message.message_id,
            "open_id": message.open_id,
            "chat_id": message.chat_id,
            "text": message.text,
        }
        try:
            response = self._client.post(
                MESSAGE_ROUTE, json=body, headers={CHANNEL_HEADER: self._secret}
            )
        except httpx.HTTPError as error:
            raise BackendUnavailable(type(error).__name__) from None
        if response.status_code in (403, 422):
            raise AdapterFault(response.status_code)
        if response.status_code != 200:
            raise BackendUnavailable(f"status {response.status_code}")
        try:
            payload = response.json()
            return Reply(duplicate=bool(payload["duplicate"]), text=str(payload["text"]))
        except (ValueError, KeyError, TypeError):
            raise BackendUnavailable("malformed reply") from None

    def close(self) -> None:
        self._client.close()


class SeenMessages:
    """Bounded set of message IDs; the oldest ID is forgotten first."""

    def __init__(self, capacity: int = SEEN_CAPACITY) -> None:
        self._capacity = capacity
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def first_time(self, message_id: str) -> bool:
        """Record the ID; false when it was already recorded."""
        with self._lock:
            if message_id in self._ids:
                return False
            self._ids[message_id] = None
            if len(self._ids) > self._capacity:
                self._ids.popitem(last=False)
            return True


def parse_event(event: Any) -> Incoming | None:
    """Turn an SDK `P2ImMessageReceiveV1` into a message; None for events to ignore.

    Only direct chats from users are relayed. A message that is not plain text is relayed with
    empty text, so the backend answers with its own request for text.
    """
    data = getattr(event, "event", None)
    message = getattr(data, "message", None)
    sender = getattr(data, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    open_id = getattr(sender_id, "open_id", None)
    message_id = getattr(message, "message_id", None)
    chat_id = getattr(message, "chat_id", None)
    if not (message_id and open_id and chat_id):
        return None
    if (
        getattr(message, "chat_type", None) != "p2p"
        or getattr(sender, "sender_type", "user") != "user"
    ):
        return None
    text = ""
    if getattr(message, "message_type", None) == "text":
        text = _text_of(getattr(message, "content", None))
    return Incoming(message_id=message_id, open_id=open_id, chat_id=chat_id, text=text)


def _text_of(content: str | None) -> str:
    try:
        value = json.loads(content or "")["text"]
    except (ValueError, KeyError, TypeError):
        return ""
    return value if isinstance(value, str) else ""


class Adapter:
    """Acknowledge fast, drop duplicates, relay on workers, reply to the same chat."""

    def __init__(self, backend: Backend, sender: Sender, workers: int = WORKERS) -> None:
        self._backend = backend
        self._sender = sender
        self._seen = SeenMessages()
        # One single-thread queue per shard keeps a user's messages in order.
        self._shards = [ThreadPoolExecutor(max_workers=1) for _ in range(workers)]

    def on_event(self, data: Any) -> None:
        """Event callback: returns at once; the work runs on a worker thread."""
        try:
            message = parse_event(data)
            if message is None:
                return
            if not self._seen.first_time(message.message_id):
                log.info("event=duplicate_dropped message_id=%s", message.message_id)
                return
            shard = self._shards[zlib.crc32(message.open_id.encode()) % len(self._shards)]
            shard.submit(self._handle, message)
        except Exception as error:
            log.error("event=dispatch_failed why=%s", type(error).__name__)

    def close(self) -> None:
        """Finish queued messages and stop the workers."""
        for shard in self._shards:
            shard.shutdown(wait=True)

    def _handle(self, message: Incoming) -> None:
        try:
            text = self._reply_text(message)
        except Exception as error:
            log.error(
                "event=handle_failed message_id=%s why=%s", message.message_id, type(error).__name__
            )
            return
        if not text:
            return
        uuid = hashlib.sha1(message.message_id.encode()).hexdigest()
        sent = self._sender.send_text(message.chat_id, text, uuid)
        log.info("event=reply message_id=%s sent=%s", message.message_id, str(sent).lower())

    def _reply_text(self, message: Incoming) -> str:
        if len(message.text) > MAX_TEXT_CHARS:
            return TOO_LONG_TEXT
        try:
            reply = self._backend.post_message(message)
        except AdapterFault as fault:
            log.error(
                "event=backend_refused message_id=%s status=%d", message.message_id, fault.status
            )
            return ""
        except BackendUnavailable as unavailable:
            log.error(
                "event=backend_unavailable message_id=%s why=%s", message.message_id, unavailable
            )
            return UNAVAILABLE_TEXT
        if reply.duplicate:
            return ""
        return reply.text


class LarkSender:
    """Sends a text message as the bot through the SDK's `im.v1.message.create`."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def send_text(self, chat_id: str, text: str, uuid: str) -> bool:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .uuid(uuid)
                .build()
            )
            .build()
        )
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                response = self._client.im.v1.message.create(request)
            except Exception as error:
                log.error("event=send_failed attempt=%d why=%s", attempt, type(error).__name__)
                continue
            if response.success():
                return True
            log.error("event=send_refused attempt=%d code=%s", attempt, response.code)
        return False


def missing_settings(settings: Settings) -> list[str]:
    """Names of the required settings that are empty."""
    required = {
        "WG_FEISHU_APP_ID": settings.feishu_app_id,
        "WG_FEISHU_APP_SECRET": settings.feishu_app_secret,
        "WG_CHANNEL_SECRET": settings.channel_secret,
    }
    return [name for name, value in required.items() if not value]


def build_stream_client(settings: Settings, adapter: Adapter) -> Any:
    """The SDK's long-connection client with `adapter.on_event` registered."""
    import lark_oapi as lark

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(adapter.on_event)
        .build()
    )
    return lark.ws.Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.WARNING,
    )


def build_sender(settings: Settings) -> LarkSender:
    import lark_oapi as lark

    client = (
        lark.Client.builder()
        .app_id(settings.feishu_app_id)
        .app_secret(settings.feishu_app_secret)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )
    return LarkSender(client)


def main(env: Mapping[str, str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings(env)
    missing = missing_settings(settings)
    if missing:
        log.error("event=config_missing names=%s", ",".join(missing))
        return 2
    backend = HttpBackend(settings.backend_url, settings.channel_secret)
    adapter = Adapter(backend, build_sender(settings))
    log.info("event=start backend=%s", settings.backend_url)
    try:
        build_stream_client(settings, adapter).start()
    except KeyboardInterrupt:
        return 0
    finally:
        adapter.close()
        backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
