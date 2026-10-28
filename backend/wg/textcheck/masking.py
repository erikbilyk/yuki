"""Mask PINs, CVVs, card numbers and step-up codes before text goes anywhere else.

Nothing here stores, logs or hashes a masked value; the result holds the redacted text and the
kinds that were redacted.
"""

from __future__ import annotations

import re

from wg.textcheck.types import MaskResult

MASK = "[已隐藏]"

PIN, CVV, CARD, CODE = "pin", "cvv", "card", "code"

_OPEN_QUOTE = r"[\"'“”‘’「」(（]?"
_OPTIONAL_LINK = rf"[\s:：=＝]*(?:是|为|is|are)?[\s:：=＝]*{_OPEN_QUOTE}"
_REQUIRED_LINK = rf"\s*(?:[:：=＝]|是|为|(?<![A-Za-z])is(?![A-Za-z]))\s*{_OPEN_QUOTE}"
_CODE_VALUE = r"(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{3,12}(?![A-Za-z0-9])"
_TOKEN_CHAR = r"[A-Za-z0-9_!@#$%^&*+=~\-]"
_PASSWORD_VALUE = rf"{_TOKEN_CHAR}{{3,32}}(?:\.{_TOKEN_CHAR}+)*"

# Latin keywords need ASCII-letter boundaries; Chinese and Cyrillic words do not.
_KEYWORDS: tuple[tuple[str, str], ...] = (
    (CVV, r"(?<![A-Za-z])(?:CVV|CVC)2?码?(?![A-Za-z])|安全码"),
    (CODE, r"验证码|动态码|校验码|短信码|口令|(?<![A-Za-z])(?:OTP|code)(?![A-Za-z])|код"),
    (PIN, r"(?<![A-Za-z])PIN码?(?![A-Za-z])|密码|(?<![A-Za-z])password(?![A-Za-z])|пароль"),
)
_PASSWORD_WORDS = r"密码|(?<![A-Za-z])password(?![A-Za-z])|пароль"
_KEYWORD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    *(
        (kind, re.compile(rf"(?:{words}){_OPTIONAL_LINK}(?P<value>{_CODE_VALUE})", re.IGNORECASE))
        for kind, words in _KEYWORDS
    ),
    (
        PIN,
        re.compile(
            rf"(?:{_PASSWORD_WORDS}){_REQUIRED_LINK}(?P<value>{_PASSWORD_VALUE})", re.IGNORECASE
        ),
    ),
)
_SPOKEN_RE = re.compile(
    r"(?<![\d.,])(?P<value>\d{4,8})(?![\d])\s*(?:是|为|is)\s*(?:我的|my|the)?\s*"
    r"(?P<keyword>验证码|动态码|校验码|密码|PIN码?|CVV2?码?|安全码|код|пароль)",
    re.IGNORECASE,
)
_SPOKEN_KINDS = {"验证码": CODE, "动态码": CODE, "校验码": CODE, "код": CODE, "安全码": CVV}

_CARD_RUN_RE = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
_CARD_GROUPED_RE = re.compile(
    r"(?<!\d)(?:\d{4}(?:[ -]\d{4}){2,3}(?:[ -]\d{1,3})?|\d{4}[ -]\d{6}[ -]\d{5})(?!\d)"
)
_SIX_DIGITS_RE = re.compile(
    r"(?<![\d.,¥￥$])\d{6}(?![\d])(?![.,]\d)(?!\s*(?:元|块|万|千|%|％|rmb|cny))", re.IGNORECASE
)

Span = tuple[int, int, str]


def luhn_valid(digits: str) -> bool:
    """Luhn checksum of a digit string."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _card_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    for pattern in (_CARD_RUN_RE, _CARD_GROUPED_RE):
        for found in pattern.finditer(text):
            digits = re.sub(r"\D", "", found.group(0))
            if 13 <= len(digits) <= 19 and luhn_valid(digits):
                spans.append((found.start(), found.end(), CARD))
    return spans


def _keyword_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    for kind, pattern in _KEYWORD_PATTERNS:
        spans += [(m.start("value"), m.end("value"), kind) for m in pattern.finditer(text)]
    for found in _SPOKEN_RE.finditer(text):
        kind = _SPOKEN_KINDS.get(found.group("keyword").lower(), PIN)
        spans.append((found.start("value"), found.end("value"), kind))
    return spans


def _merge(spans: list[Span]) -> list[tuple[int, int, tuple[str, ...]]]:
    """Join overlapping spans; a joined span keeps every kind that reached it."""
    merged: list[tuple[int, int, tuple[str, ...]]] = []
    for start, end, kind in sorted(spans):
        if merged and start < merged[-1][1]:
            first, last, kinds = merged[-1]
            merged[-1] = (first, max(end, last), kinds if kind in kinds else (*kinds, kind))
        else:
            merged.append((start, end, (kind,)))
    return merged


def mask_secrets(text: str, *, challenge_open: bool = False) -> MaskResult:
    """Mask the secrets in the text.

    With a challenge open, any standalone six-digit number is taken for a step-up code.
    """
    spans = _card_spans(text) + _keyword_spans(text)
    if challenge_open:
        spans += [(m.start(), m.end(), CODE) for m in _SIX_DIGITS_RE.finditer(text)]
    pieces: list[str] = []
    kinds: list[str] = []
    cursor = 0
    for start, end, span_kinds in _merge(spans):
        pieces += [text[cursor:start], MASK]
        cursor = end
        kinds += [kind for kind in span_kinds if kind not in kinds]
    pieces.append(text[cursor:])
    return MaskResult(text="".join(pieces), kinds=tuple(kinds))
