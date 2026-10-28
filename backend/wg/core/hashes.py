"""Canonical JSON and the hashes built on it (SPEC §5.4)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"not canonical JSON: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """UTF-8 text with sorted keys and no whitespace; Decimals and dates as strings.

    Floats are refused: no money, share or percent value may be a float (SPEC §5.1).
    """
    _refuse_floats(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_encode
    )


def _refuse_floats(value: Any) -> None:
    if isinstance(value, float):
        raise TypeError("float in canonical JSON")
    if isinstance(value, dict):
        for item in value.values():
            _refuse_floats(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _refuse_floats(item)


def sha256_hex(value: Any) -> str:
    """SHA-256 of the canonical JSON of `value`, as 64 hex digits."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def hmac_hex(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
