"""Channel routes: the customer's pairing code and the Feishu adapter's message route (SPEC §21)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from wg.api.guards import route_guard
from wg.contracts.api import ChannelReply, FeishuMessage, PairingCodeView
from wg.core.k1_session import Principal
from wg.im import binding
from wg.im.service import ImService

router = APIRouter(prefix="/v1/channels")

PairingRequester = Annotated[Principal, Depends(route_guard("POST", "/v1/channels/pairing-code"))]
AdapterGuard = Depends(route_guard("POST", "/v1/channels/feishu/messages"))


@router.post("/pairing-code", status_code=status.HTTP_201_CREATED)
def pairing_code(request: Request, principal: PairingRequester) -> PairingCodeView:
    return binding.issue_pairing_code(
        request.app.state.db, request.app.state.settings.code_hmac_key, principal.customer_id
    )


@router.post("/feishu/messages", dependencies=[AdapterGuard])
def feishu_message(body: FeishuMessage, request: Request) -> ChannelReply:
    service: ImService = request.app.state.im
    return service.handle(body)
