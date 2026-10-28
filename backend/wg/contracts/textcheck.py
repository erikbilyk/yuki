"""Result types of the text helpers in `wg.textcheck` (amounts and masking)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import NonNegativeInt, StrictBool, model_validator

from wg.contracts._base import ContractModel
from wg.contracts.money import Fen


class Comparator(StrEnum):
    """A limit written next to an amount: 不超过/最多 is `<=`, 低于/少于 is `<`."""

    LE = "<="
    LT = "<"
    GE = ">="
    GT = ">"


_STRICT = frozenset({Comparator.LT, Comparator.GT})


class AmountMatch(ContractModel):
    """An amount found in text, in fen; [start, end) covers the amount, not the comparator.

    `strict` is True exactly for the exclusive comparators `<` and `>`.
    """

    fen: Fen
    start: NonNegativeInt
    end: NonNegativeInt
    text: str
    comparator: Comparator | None = None
    strict: StrictBool = False

    @model_validator(mode="after")
    def _consistent(self) -> AmountMatch:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.strict != (self.comparator in _STRICT):
            raise ValueError("strict is set exactly for the comparators < and >")
        return self


class MaskKind(StrEnum):
    PIN = "pin"
    CVV = "cvv"
    CODE = "code"
    CARD = "card"


class MaskResult(ContractModel):
    """Masked text and the kinds of secret replaced; no masked value is kept."""

    text: str
    kinds: tuple[MaskKind, ...] = ()
