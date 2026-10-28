"""Which pack handles which action kind (SPEC §12.1)."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from wg.contracts.enums import ActionKind
from wg.contracts.errors import ErrorCode, WgError
from wg.packs.base import ActionPack
from wg.packs.transfer import TransferPack

_PACKS: tuple[ActionPack, ...] = (TransferPack(),)

PACKS: Mapping[ActionKind, ActionPack] = MappingProxyType(
    {kind: pack for pack in _PACKS for kind in pack.kinds}
)


def pack_for(kind: ActionKind) -> ActionPack:
    pack = PACKS.get(ActionKind(kind))
    if pack is None:
        raise WgError(ErrorCode.UNKNOWN_ACTION_KIND, {"kind": ActionKind(kind).value})
    return pack
