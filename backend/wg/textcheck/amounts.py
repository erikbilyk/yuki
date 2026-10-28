"""Independent parser for money amounts in Chinese, English and Russian text.

The parser works on the customer's own words and returns amounts in fen with their spans, so
that an amount cited by the model can be compared with what the text actually says.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from wg.textcheck.types import AmountMatch

QuoteVerdict = Literal["ok", "AMOUNT_MISMATCH", "MISSING_SLOT"]
Span = tuple[int, int]

LE, LT, GE, GT = "<=", "<", ">=", ">"
STRICT_COMPARATORS = frozenset({LT, GT})

_COMPARATOR_WORDS: dict[str, tuple[str, ...]] = {
    LE: ("不超过", "不高于", "不大于", "最多", "至多", "≤", "<=", "=<", "≦"),
    LT: ("低于", "少于", "小于", "<", "＜"),
    GE: ("不低于", "不少于", "不小于", "至少", "最少", "≥", ">=", "≧"),
    GT: ("高于", "多于", "超过", "大于", ">", "＞"),
}
_COMPARATOR_PHRASES: dict[str, tuple[str, ...]] = {
    LE: ("at most", "no more than", "not more than", "up to", "не более", "не больше", "максимум"),
    LT: ("less than", "fewer than", "under", "below", "менее", "меньше"),
    GE: ("at least", "no less than", "not less than", "не менее", "не меньше", "минимум"),
    GT: ("more than", "greater than", "over", "above", "более", "больше", "свыше"),
}


_COMPARATORS: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            (word, op)
            for table in (_COMPARATOR_WORDS, _COMPARATOR_PHRASES)
            for op, words in table.items()
            for word in words
        ),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)
_LINKING_WORD_RE = re.compile(r"\s*(?:为|是)?\s*$")

_CN_DIGITS = {c: i for i, c in enumerate("零一二三四五六七八九")} | {"〇": 0, "两": 2}
_CN_FINANCIAL = str.maketrans("壹贰叁肆伍陆柒捌玖拾佰仟", "一二三四五六七八九十百千")
_CN_MAGNITUDE = {"十": 10, "百": 100, "千": 1000}
_CN_CHARS = "零〇一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟"

_ARABIC_OR_CN = re.compile(
    rf"\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?|[{_CN_CHARS}]+",
)
_UNIT_RE = re.compile(
    r"\s*(?P<unit>千|万|百|k|w|thousand|тыс(?:яч[а-я]*|\.)?)(?![克米瓦])",
    re.IGNORECASE,
)
_UNIT_VALUE = {"千": 1000, "万": 10000, "百": 100, "k": 1000, "w": 10000, "thousand": 1000}
_RUSSIAN_THOUSAND_PREFIX = "тыс"
_CURRENCY_RE = re.compile(
    r"\s*(?:块钱|块|元|圆|人民币|rmb|cny|yuan|руб(?:лей|ля|ль)?\.?|р\.|₽)(?:整)?",
    re.IGNORECASE,
)
_CURRENCY_PREFIX_RE = re.compile(r"(?:[¥￥$₽]|人民币|rmb|cny)\s*$", re.IGNORECASE)

# A bare number followed by one of these is a count, date part or score, not money.
_COUNT_SUFFIX = frozenset("年月日号天周笔个次人位份只条张岁倍折成点时分秒季期楼层页项名家台件套")
_PERCENT_SUFFIX_RE = re.compile(r"\s*(?:[%％]|个百分点|个点|percent|pct)", re.IGNORECASE)
_NON_MONEY_PREFIX_RE = re.compile(
    r"(?:尾号|末四位|末尾|后四位|后4位|卡号|账号|账户号|编号|代码|单号|订单号|流水号|第|No\.?|#|百分之)\s*[:：]?\s*$",
    re.IGNORECASE,
)
_NON_MONEY_SUFFIX_RE = re.compile(r"\s*(?:基金|号)")

_NON_AMOUNT_SPANS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}[日号]?)?",
        r"\d{4}\.\d{1,2}\.\d{1,2}",
        r"(?<!\d)\d{1,2}[-/]\d{1,2}[-/]\d{2,4}",
        r"(?<!\d)\d{1,2}月\d{1,2}[日号]?",
        r"(?<!\d)\d{1,2}/\d{1,2}(?!\d)",
        r"(?<!\d)\d{1,2}[:：]\d{2}(?:[:：]\d{2})?",
        r"\bT\s*\+\s*\d+",
        r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d[ -]?\d{4}[ -]?\d{4}(?!\d)",
        r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)",
        r"(?<!\d)\d{3,4}-\d{3,4}-\d{3,4}(?!\d)",
    )
)

_MAX_PLAIN_DIGITS = 10


def parse_chinese_numeral(token: str) -> int | None:
    """Value of a Chinese numeral such as 三千四百, 两千, 一万二 or 十五; None when not a number."""
    token = token.translate(_CN_FINANCIAL)
    total = section = 0
    digit: int | None = None
    last_unit = 0
    zero_after_unit = False
    seen_wan = False
    for char in token:
        if char in _CN_DIGITS:
            value = _CN_DIGITS[char]
            if value == 0:
                if digit is not None:
                    return None
                zero_after_unit = True
                continue
            if digit is not None:
                return None
            digit = value
        elif char in _CN_MAGNITUDE:
            if digit is None:
                if char != "十" or section:
                    return None
                digit = 1
            section += digit * _CN_MAGNITUDE[char]
            digit = None
            last_unit = _CN_MAGNITUDE[char]
            zero_after_unit = False
        else:
            if seen_wan:
                return None
            if digit is not None:
                section += digit
                digit = None
            if not section:
                return None
            total, section = section * 10000, 0
            seen_wan = True
            last_unit = 10000
            zero_after_unit = False
    if digit is not None:
        if last_unit and not zero_after_unit:
            section += digit * max(last_unit // 10, 1)
        else:
            section += digit
    result = total + section
    return result or None


def _unit_multiplier(word: str) -> int:
    word = word.lower()
    return 1000 if word.startswith(_RUSSIAN_THOUSAND_PREFIX) else _UNIT_VALUE[word]


def _to_fen(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _excluded_spans(text: str) -> list[Span]:
    return [m.span() for pattern in _NON_AMOUNT_SPANS for m in pattern.finditer(text)]


def _overlaps(a: Span, b: Span) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _comparator_before(text: str, start: int) -> str | None:
    """The limit operator written right before an amount, preferring the longest wording."""
    head = _LINKING_WORD_RE.sub("", text[max(0, start - 24) : start]).lower()
    for word, op in _COMPARATORS:
        if not head.endswith(word):
            continue
        before = head[: len(head) - len(word)]
        if word[-1].isalpha() and before[-1:].isalnum() and before[-1:].isascii():
            continue
        return op
    return None


def _read_number(token: str) -> Decimal | None:
    if token[0].isdigit():
        return Decimal(token.replace(",", ""))
    value = parse_chinese_numeral(token)
    return None if value is None else Decimal(value)


def find_amounts(text: str) -> list[AmountMatch]:
    """Every money amount in the text, in order of position, with comparator and span."""
    excluded = _excluded_spans(text)
    matches: list[AmountMatch] = []
    cursor = 0
    for found in _ARABIC_OR_CN.finditer(text):
        if found.start() < cursor:
            continue
        token = found.group(0)
        if any(_overlaps(found.span(), span) for span in excluded):
            continue
        if token[0].isdigit() and len(token.replace(",", "").split(".")[0]) >= _MAX_PLAIN_DIGITS:
            continue
        number = _read_number(token)
        if number is None:
            continue

        start, end = found.start(), found.end()
        multiplier = 1
        has_marker = False
        unit = _UNIT_RE.match(text, end) if token[0].isdigit() else None
        if unit is not None:
            multiplier = _unit_multiplier(unit.group("unit"))
            end = unit.end()
            has_marker = True
        currency = _CURRENCY_RE.match(text, end)
        if currency is not None:
            end = currency.end()
            has_marker = True
        prefix = _CURRENCY_PREFIX_RE.search(text[max(0, start - 4) : start])
        if prefix is not None:
            start -= len(prefix.group(0))
            has_marker = True

        if _PERCENT_SUFFIX_RE.match(text, end) or _NON_MONEY_PREFIX_RE.search(text[:start]):
            continue
        following = text[end : end + 1]
        if following.isascii() and following.isalpha():
            continue
        if not has_marker:
            if following in _COUNT_SUFFIX:
                continue
            if _NON_MONEY_SUFFIX_RE.match(text, end):
                continue
            if not token[0].isdigit() and len(token) == 1:
                continue

        comparator = _comparator_before(text, start)
        fen = _to_fen(number * multiplier)
        if fen <= 0:
            continue
        matches.append(
            AmountMatch(
                fen=fen,
                start=start,
                end=end,
                text=text[start:end],
                comparator=comparator,
                strict=comparator in STRICT_COMPARATORS,
            )
        )
        cursor = end
    return matches


def amounts_outside(text: str, remark_span: Span | None) -> list[AmountMatch]:
    """Amounts in the text that do not touch the remark span (start, end exclusive)."""
    found = find_amounts(text)
    if remark_span is None:
        return found
    return [m for m in found if not _overlaps((m.start, m.end), remark_span)]


def _occurrences(text: str, needle: str) -> list[Span]:
    spans: list[Span] = []
    index = text.find(needle)
    while needle and index != -1:
        spans.append((index, index + len(needle)))
        index = text.find(needle, index + 1)
    return spans


def check_quote(
    utterance: str, quote: str, amount_fen: int, remark_span: Span | None
) -> QuoteVerdict:
    """Compare the fragment the model cites as the amount with what the utterance says.

    The utterance must hold exactly one distinct amount outside the remark, the quote must
    point at it, and the quote must parse to the amount the model passed.
    """
    candidates = amounts_outside(utterance, remark_span)
    if len({m.fen for m in candidates}) != 1:
        return "MISSING_SLOT"
    quoted = find_amounts(quote)
    if len({m.fen for m in quoted}) != 1 or quoted[0].fen != amount_fen:
        return "AMOUNT_MISMATCH"
    outside = [
        span
        for span in _occurrences(utterance, quote)
        if remark_span is None or not _overlaps(span, remark_span)
    ]
    if not any(_overlaps(span, (m.start, m.end)) for span in outside for m in candidates):
        return "AMOUNT_MISMATCH"
    if candidates[0].fen != amount_fen:
        return "AMOUNT_MISMATCH"
    return "ok"


def limit_satisfied(limit: AmountMatch, value_fen: int) -> bool:
    """Whether a value meets the comparator written in the text; a bare amount means equal."""
    match limit.comparator:
        case "<=":
            return value_fen <= limit.fen
        case "<":
            return value_fen < limit.fen
        case ">=":
            return value_fen >= limit.fen
        case ">":
            return value_fen > limit.fen
        case _:
            return value_fen == limit.fen
