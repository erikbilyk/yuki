"""Inbound IM messages: pairing, duplicate delivery, the agent turn and the approval refusal.

Approvals and codes never happen in IM (SPEC §21): a message that tries one is answered with
a link to the trusted card and never reaches the model. A message is handled at most once;
its ID is recorded before anything else, so a redelivery gets an empty duplicate reply.
"""

from __future__ import annotations

import re

from wg.agent.chat import ChatService, shown_text
from wg.contracts.api import ChannelReply, FeishuMessage
from wg.contracts.enums import AuditActor, Channel
from wg.contracts.errors import ErrorCode, WgError
from wg.core.k2_context import challenge_open
from wg.im import binding
from wg.k6 import audit, channel
from wg.k6.db import Database
from wg.k6.simtime import now
from wg.textcheck.masking import CODE, mask_secrets

UNBOUND_TEXT = (
    "您还没有绑定账户。请在网页版生成绑定码，然后在这里发送“绑定 123456”（换成您的绑定码）。"
)
BOUND_TEXT = "绑定成功。现在可以在这里查询和提出需求；确认操作和验证码需要在网页版完成。"
BIND_FAILED_TEXT = "绑定码无效或已过期。请在网页版重新生成绑定码。"
BLANK_TEXT = "请发送文字消息。"
UNAVAILABLE_TEXT = "服务暂时不可用，请稍后再试。"
APPROVAL_REFUSED_TEXT = "为了资金安全，确认操作和验证码只能在网页版完成，请不要在聊天里发送验证码。"
CARD_LINK_TEXT = "请打开链接核对并确认："
HOME_LINK_TEXT = "请登录网页版查看待确认事项："
NEEDS_CARD_TEXT = "这项操作需要您在网页版核对并确认；验证码也在网页里输入："

_PUNCTUATION = r"[\s，,。.!！?？~、]"
_APPROVE_WORD = r"(?:确认|确定|批准|同意|授权|放行|批了|执行)"
_FILLER = (
    r"(?:请|麻烦|我|好的?|是的?|对|一下|吧|了|无误|这笔|该笔|此笔|转账|付款|支付|交易|操作|申请|"
    r"计划|订单|购买|赎回|卡片|方案)"
)
# A message made only of approval words, filler and punctuation.
_BARE_APPROVAL = re.compile(rf"^(?:{_PUNCTUATION}|{_FILLER}|{_APPROVE_WORD})+$")
_APPROVAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:确认|批准|同意|授权|放行).{0,6}(?:转账|付款|支付|转|付)(?!.{0,2}(?:记录|状态|结果|情况|进度|明细))",
        r"(?<![A-Za-z])(?:approve|authori[sz]e|confirm)(?![A-Za-z])",
        rf"^{_PUNCTUATION}*(?:ok|okay|yes|y|sure|go ahead){_PUNCTUATION}*$",
    )
)


def is_approval_attempt(text: str) -> bool:
    """Whether the message approves or confirms something instead of asking for it."""
    if _BARE_APPROVAL.match(text) and re.search(_APPROVE_WORD, text):
        return True
    return any(pattern.search(text) for pattern in _APPROVAL_PATTERNS)


class ImService:
    def __init__(self, db: Database, chat: ChatService, *, code_key: str, web_base_url: str):
        self.db = db
        self.chat = chat
        self.code_key = code_key
        self.web_base_url = web_base_url.rstrip("/")

    def handle(self, message: FeishuMessage) -> ChannelReply:
        first = self.db.run_in_uow(
            lambda uow: channel.mark_seen(uow, binding.FEISHU, message.message_id),
            "record im message",
        )
        with self.db.read() as conn:
            bound = channel.active_binding(conn, binding.FEISHU, message.open_id)
        if not first:
            return self._reply(message, "", bound=bound is not None, duplicate=True)

        code = binding.parse_bind_command(message.text)
        if code is not None:
            customer_id = binding.bind_user(self.db, self.code_key, message.open_id, code)
            text = BOUND_TEXT if customer_id else BIND_FAILED_TEXT
            return self._reply(message, text, bound=customer_id is not None)
        if bound is None:
            self._audit(AuditActor.SYSTEM, "im_unbound_message", None, message, None)
            return self._reply(message, UNBOUND_TEXT, bound=False)
        customer_id = bound.customer_id
        if not message.text.strip():
            return self._reply(message, BLANK_TEXT, bound=True)
        if self._attempts_approval(customer_id, message.text):
            return self._refuse_approval(customer_id, message)
        return self._answer(customer_id, message)

    def _attempts_approval(self, customer_id: str, text: str) -> bool:
        with self.db.read() as conn:
            open_code = challenge_open(conn, customer_id)
        masked = mask_secrets(text, challenge_open=open_code)
        return CODE in masked.kinds or is_approval_attempt(text)

    def _refuse_approval(self, customer_id: str, message: FeishuMessage) -> ChannelReply:
        self._audit(
            AuditActor.CUSTOMER,
            "im_approval_refused",
            customer_id,
            message,
            ErrorCode.FORBIDDEN_ROUTE.value,
        )
        with self.db.read() as conn:
            card_id = channel.newest_unapproved_card(conn, customer_id, now(conn))
        if card_id is None:
            link = self.web_base_url
            lead = HOME_LINK_TEXT
        else:
            link = self.card_link(card_id)
            lead = CARD_LINK_TEXT
        return self._reply(
            message, f"{APPROVAL_REFUSED_TEXT}\n{lead}{link}", bound=True, links=(link,)
        )

    def _answer(self, customer_id: str, message: FeishuMessage) -> ChannelReply:
        try:
            reply = self.chat.converse(
                customer_id, f"{binding.FEISHU}:{message.open_id}", message.text, channel=Channel.IM
            )
        except WgError as error:
            if error.code is not ErrorCode.PROVIDER_TIMEOUT:
                raise
            return self._reply(message, UNAVAILABLE_TEXT, bound=True)
        text = shown_text(reply)
        links = tuple(self.card_link(card_id) for card_id in reply.card_ids)
        if links:
            text = "\n".join([text, NEEDS_CARD_TEXT, *links])
        return self._reply(message, text, bound=True, links=links)

    def card_link(self, card_id: str) -> str:
        return f"{self.web_base_url}/cards/{card_id}"

    def _audit(
        self,
        actor: AuditActor,
        event: str,
        customer_id: str | None,
        message: FeishuMessage,
        decision: str | None,
    ) -> None:
        self.db.run_in_uow(
            lambda uow: audit.record(
                uow,
                actor,
                event,
                customer_id=customer_id,
                ref=message.message_id[:64],
                decision=decision,
            ),
            "audit im message",
        )

    @staticmethod
    def _reply(
        message: FeishuMessage,
        text: str,
        *,
        bound: bool,
        duplicate: bool = False,
        links: tuple[str, ...] = (),
    ) -> ChannelReply:
        return ChannelReply(
            message_id=message.message_id,
            duplicate=duplicate,
            bound=bound,
            text=text,
            links=links,
        )
