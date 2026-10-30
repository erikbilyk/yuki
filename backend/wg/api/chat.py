"""Chat routes: one agent turn per message, and the stored masked history (SPEC §19.2)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from wg.agent.chat import ChatService
from wg.api.guards import route_guard
from wg.contracts.api import ChatHistoryQuery, ChatHistoryView, ChatReply, ChatRequest
from wg.core.k1_session import Principal

router = APIRouter(prefix="/v1/chat")

Sender = Annotated[Principal, Depends(route_guard("POST", "/v1/chat/messages"))]
HistoryReader = Annotated[Principal, Depends(route_guard("GET", "/v1/chat/history"))]


def _service(request: Request) -> ChatService:
    return request.app.state.chat


@router.post("/messages")
def send_message(body: ChatRequest, request: Request, principal: Sender) -> ChatReply:
    return _service(request).send(principal.session, body.text)


@router.get("/history")
def history(
    request: Request, principal: HistoryReader, query: Annotated[ChatHistoryQuery, Query()]
) -> ChatHistoryView:
    return _service(request).history(principal.customer_id, query.limit)
