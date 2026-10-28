"""Money in integer fen and Decimal values that travel as JSON strings."""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, PlainSerializer, Strict, WithJsonSchema

Fen = Annotated[int, Strict()]
NonNegativeFen = Annotated[int, Strict(), Field(ge=0)]
PositiveFen = Annotated[int, Strict(), Field(gt=0)]

_DECIMAL_TEXT = re.compile(r"-?[0-9]+(?:\.([0-9]+))?")
_ONE_FEN = Decimal("0.01")


def _decimal_validator(places: int) -> Callable[[Any], Decimal]:
    exponent = Decimal(1).scaleb(-places)

    def validate(value: Any) -> Decimal:
        # bool is an int subclass and int is refused too: Decimals arrive as strings.
        if isinstance(value, str):
            match = _DECIMAL_TEXT.fullmatch(value)
            if match is None:
                raise ValueError("expected a plain decimal string such as '39.5089'")
            fraction = match.group(1) or ""
            if len(fraction) > places:
                raise ValueError(f"at most {places} decimal places")
            number = Decimal(value)
        elif isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError("NaN and infinity are not allowed")
            number = value
        else:
            raise ValueError(f"expected a decimal string, got {type(value).__name__}")
        try:
            normalised = number.quantize(exponent)
        except InvalidOperation:
            raise ValueError("decimal value out of range") from None
        if normalised != number:
            raise ValueError(f"at most {places} decimal places")
        return normalised

    return validate


def _serialize_decimal(value: Decimal) -> str:
    return format(value, "f")


def DecimalStr(places: int) -> Any:
    """Decimal with at most `places` decimals, normalised to exactly `places`, sent as a string."""
    pattern = rf"^-?[0-9]+(\.[0-9]{{1,{places}}})?$" if places else r"^-?[0-9]+$"
    return Annotated[
        Decimal,
        BeforeValidator(_decimal_validator(places)),
        PlainSerializer(_serialize_decimal, return_type=str),
        WithJsonSchema({"type": "string", "pattern": pattern}),
    ]


Shares = DecimalStr(4)
Nav = DecimalStr(4)
Rate = DecimalStr(4)
Weight = DecimalStr(4)
Pct = DecimalStr(4)


def quantize_fen(yuan: Decimal) -> int:
    """Round an amount in yuan to whole fen, half away from zero."""
    if not isinstance(yuan, Decimal) or not yuan.is_finite():
        raise TypeError("quantize_fen expects a finite Decimal amount in yuan")
    return int((yuan / _ONE_FEN).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def percent_str(value: Decimal, places: int = 4) -> str:
    """Format a percent value (39.50891 means 39.50891 %) half-up to `places` decimals."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError("percent_str expects a finite Decimal")
    return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), "f")
