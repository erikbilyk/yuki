"""Which pack handles which action kind (SPEC §12.1)."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from wg.contracts.enums import ActionKind
from wg.contracts.errors import ErrorCode, WgError
from wg.packs.base import ActionPack, Pack, StatePack
from wg.packs.bills import BillsPack
from wg.packs.funds import CheckpointPack, FundsPack
from wg.packs.mandates import DebitPayPack
from wg.packs.transfer import TransferPack

_PACKS: tuple[Pack, ...] = (
    TransferPack(),
    BillsPack(),
    FundsPack(),
    CheckpointPack(),
    DebitPayPack(),
)

PACKS: Mapping[ActionKind, Pack] = MappingProxyType(
    {kind: pack for pack in _PACKS for kind in pack.kinds}
)


def pack_for(kind: ActionKind) -> Pack:
    pack = PACKS.get(ActionKind(kind))
    if pack is None:
        raise WgError(ErrorCode.UNKNOWN_ACTION_KIND, {"kind": ActionKind(kind).value})
    return pack


def money_pack(kind: ActionKind) -> ActionPack:
    """The pack of a kind that moves money; a state-only kind has no operation to settle."""
    pack = pack_for(kind)
    if isinstance(pack, StatePack):
        raise WgError(ErrorCode.UNKNOWN_ACTION_KIND, {"kind": ActionKind(kind).value})
    return pack  # type: ignore[return-value]
