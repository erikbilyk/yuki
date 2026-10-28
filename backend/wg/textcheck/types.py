"""Result types returned by the text-check helpers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt


class AmountMatch(BaseModel):
    """A money amount found in free text, in fen, with its span (end exclusive)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fen: StrictInt
    start: StrictInt
    end: StrictInt
    text: str
    comparator: str | None = None
    strict: StrictBool = False


class MaskResult(BaseModel):
    """Text with secrets replaced, and the kinds of secret that were replaced."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    kinds: tuple[str, ...] = ()
