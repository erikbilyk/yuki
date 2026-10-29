"""K2: what reaches the model and where each slot of a draft came from (SPEC §18.3, §18.6).

Customer text is masked before anything else sees it. A draft value counts as the customer's
only when the fragment the model quotes for it lies in the customer's message outside the
payment remark; amounts and limits are then parsed independently and must equal the value the
model passed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel

from wg.contracts.enums import Provenance
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.textcheck import MaskKind
from wg.k6 import challenges
from wg.k6.db import Reader
from wg.textcheck.amounts import check_quote, find_amounts
from wg.textcheck.masking import mask_secrets

Span = tuple[int, int]

# Words that open a payment note; everything after one is the customer's opaque remark.
REMARK_MARKERS = ("备注", "附言", "留言", "remark", "note", "комментарий", "назначение")
# Words that make a written percent limit exclusive ("低于 35%"); others are inclusive.
STRICT_LIMIT_WORDS = ("低于", "少于", "小于", "不到", "<", "＜", "less than", "below", "менее")
# Keys the model never needs; dropped from every payload as a second line behind the views.
HIDDEN_KEYS = frozenset(
    {"terms_hash", "scope_hash", "code_hmac", "csrf_token", "password", "token_hash"}
)

_PERCENT_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[%％]|百分之(\d+(?:\.\d+)?)")
_MARKER_RE = re.compile("|".join(re.escape(m) for m in REMARK_MARKERS), re.IGNORECASE)


@dataclass(frozen=True)
class Ingress:
    """A customer message after masking; `text` is the only form that is kept or sent on."""

    text: str
    masked: tuple[MaskKind, ...]


def mask_ingress(text: str, *, challenge_open: bool) -> Ingress:
    """Mask PINs, CVVs, card numbers and codes; with a challenge open, any six digits too."""
    result = mask_secrets(text, challenge_open=challenge_open)
    return Ingress(text=result.text, masked=result.kinds)


def challenge_open(reader: Reader, customer_id: str) -> bool:
    """Whether the customer has a code outstanding, so a bare six-digit number is masked."""
    return any(c.is_open for c in challenges.active_challenges(reader, customer_id=customer_id))


def _occurrences(text: str, needle: str) -> list[Span]:
    spans: list[Span] = []
    index = text.find(needle) if needle else -1
    while index != -1:
        spans.append((index, index + len(needle)))
        index = text.find(needle, index + 1)
    return spans


def _overlaps(a: Span, b: Span) -> bool:
    return a[0] < b[1] and b[0] < a[1]


@dataclass(frozen=True)
class Utterance:
    """The masked message of the turn and the span of its payment remark, if any.

    The remark runs from the first remark marker to the end of the message. A remark the
    model copies into a draft widens the span to cover where that text appears, never
    narrows it, so a model can hide an amount from the check but never expose one.
    """

    text: str
    remark_span: Span | None = None

    @classmethod
    def of(cls, text: str, remark: str | None = None) -> Utterance:
        spans: list[Span] = []
        marker = _MARKER_RE.search(text)
        if marker is not None:
            spans.append((marker.start(), len(text)))
        if remark:
            spans += _occurrences(text, remark)
        if not spans:
            return cls(text)
        return cls(text, (min(s[0] for s in spans), max(s[1] for s in spans)))

    def provenance(self, quote: str | None) -> Provenance:
        """`user_utterance` when the quote appears in the message outside the remark."""
        if not quote:
            return Provenance.UNTRUSTED_TEXT
        outside = [
            span
            for span in _occurrences(self.text, quote)
            if self.remark_span is None or not _overlaps(span, self.remark_span)
        ]
        return Provenance.USER_UTTERANCE if outside else Provenance.UNTRUSTED_TEXT

    def check_amount(self, slot: str, quote: str, amount_fen: int) -> None:
        """Refuse an amount the message does not state exactly once outside the remark."""
        verdict = check_quote(self.text, quote, amount_fen, self.remark_span)
        if verdict != "ok":
            raise WgError(ErrorCode(verdict), {"slots": [slot]})

    def check_percent(self, slot: str, quote: str, value: Decimal) -> None:
        """The percent limit counterpart of `check_amount`."""
        stated = {p for p, span in _percents(self.text) if not self._in_remark(span)}
        if len(stated) != 1:
            raise WgError(ErrorCode.MISSING_SLOT, {"slots": [slot]})
        quoted = {p for p, _ in _percents(quote)}
        if quoted != stated or quoted != {value.normalize()}:
            raise WgError(ErrorCode.AMOUNT_MISMATCH, {"slots": [slot]})

    def _in_remark(self, span: Span) -> bool:
        return self.remark_span is not None and _overlaps(span, self.remark_span)


def _percents(text: str) -> list[tuple[Decimal, Span]]:
    found: list[tuple[Decimal, Span]] = []
    for match in _PERCENT_RE.finditer(text):
        try:
            value = Decimal(match.group(1) or match.group(2))
        except InvalidOperation:
            continue
        found.append((value.normalize(), match.span()))
    return found


def limit_is_strict(quote: str) -> bool:
    """Whether a quoted limit excludes its value: "低于 35%" yes, "不超过 35%" no."""
    money = find_amounts(quote)
    if money and money[0].comparator is not None:
        return money[0].strict
    lowered = quote.lower()
    return any(word in lowered for word in STRICT_LIMIT_WORDS)


def model_payload(body: BaseModel) -> str:
    """The JSON the model is shown for a tool result: hidden keys dropped, secrets masked."""
    return json.dumps(_minimal(body.model_dump(mode="json")), ensure_ascii=False)


def _minimal(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _minimal(v) for k, v in value.items() if k not in HIDDEN_KEYS}
    if isinstance(value, list):
        return [_minimal(item) for item in value]
    if isinstance(value, str):
        return mask_secrets(value).text
    return value
