"""Result types of the text helpers in `wg.textcheck` (amounts and masking)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import NonNegativeInt, StrictBool, model_validator

from wg.contracts._base import ContractModel
from wg.contracts.money import Fen


class Comparator(StrEnum):
    """A limit written next to an amount: `upper` for 不超过/最多/≤, `lower` for 至少/≥."""

    UPPER = "upper"
    LOWER = "lower"


class AmountMatch(ContractModel):
    """An amount found in text, in fen, with its span [start, end).

    `strict` marks an exclusive comparator (低于, 少于, <); it is False without a comparator.
    """

    fen: Fen
    start: NonNegativeInt
    end: NonNegativeInt
    text: str
    comparator: Comparator | None
    strict: StrictBool

    @model_validator(mode="after")
    def _consistent(self) -> AmountMatch:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.strict and self.comparator is None:
            raise ValueError("strict needs a comparator")
        return self


class MaskKind(StrEnum):
    PIN = "pin"
    CVV = "cvv"
    CARD_NUMBER = "card_number"
    STEPUP_CODE = "stepup_code"


class MaskResult(ContractModel):
    """Masked text and the kinds of secret replaced; no masked value is kept."""

    text: str
    kinds: tuple[MaskKind, ...]
