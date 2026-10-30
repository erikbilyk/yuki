"""Chat messages (SPEC §7, §18.6). Only masked text is ever stored."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from wg.contracts.api import ChatRole
from wg.contracts.enums import Channel
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, now_db


@dataclass(frozen=True)
class ChatMessageRow:
    message_id: int
    session_id: str
    customer_id: str
    channel: Channel
    role: ChatRole
    masked_text: str
    k7_replaced: bool
    created_at: datetime


def _message(row) -> ChatMessageRow:
    return ChatMessageRow(
        message_id=row["message_id"],
        session_id=row["session_id"],
        customer_id=row["customer_id"],
        channel=Channel(row["channel"]),
        role=ChatRole(row["role"]),
        masked_text=row["masked_text"],
        k7_replaced=bool(row["k7_replaced"]),
        created_at=from_db(row["created_at"]),
    )


def insert_message(
    uow: UnitOfWork,
    *,
    session_id: str,
    customer_id: str,
    channel: Channel,
    role: ChatRole,
    masked_text: str,
    k7_replaced: bool = False,
) -> None:
    uow.execute(
        "INSERT INTO chat_messages (session_id, customer_id, channel, role, masked_text,"
        " k7_replaced, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            customer_id,
            Channel(channel).value,
            ChatRole(role).value,
            masked_text,
            int(k7_replaced),
            now_db(uow),
        ),
    )


def customer_messages(
    reader: Reader, customer_id: str, limit: int, *, session_id: str | None = None
) -> list[ChatMessageRow]:
    """The customer's newest `limit` messages, optionally of one session, oldest first."""
    sql = "SELECT * FROM chat_messages WHERE customer_id = ?"
    params: tuple[object, ...] = (customer_id,)
    if session_id is not None:
        sql += " AND session_id = ?"
        params += (session_id,)
    rows = reader.execute(sql + " ORDER BY message_id DESC LIMIT ?", (*params, limit)).fetchall()
    return [_message(row) for row in reversed(rows)]
