"""Amount parser: forms, units, comparators, false positives and the quote cross-check."""

from __future__ import annotations

import pytest

from wg.textcheck.amounts import (
    amounts_outside,
    check_quote,
    find_amounts,
    limit_satisfied,
    parse_chinese_numeral,
)


def fens(text: str) -> list[int]:
    return [m.fen for m in find_amounts(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("500", 50000),
        ("3,400", 340000),
        ("3400.50", 340050),
        ("¥3,400", 340000),
        ("￥3400", 340000),
        ("3400元", 340000),
        ("500块", 50000),
        ("500块钱", 50000),
        ("人民币500", 50000),
        ("500 RMB", 50000),
        ("500 yuan", 50000),
        ("0.01元", 1),
        ("五百", 50000),
        ("三千四百", 340000),
        ("两千", 200000),
        ("一万二", 1200000),
        ("两万五千", 2500000),
        ("一百零五", 10500),
        ("十五元", 1500),
        ("伍佰元", 50000),
        ("5千", 500000),
        ("1.2万", 1200000),
        ("1.2万元", 1200000),
        ("5k", 500000),
        ("5K", 500000),
        ("5 тыс.", 500000),
        ("5 тысяч рублей", 500000),
        ("3400 руб.", 340000),
        ("₽3400", 340000),
        ("2 thousand", 200000),
    ],
)
def test_single_amount_forms(text: str, expected: int) -> None:
    assert fens(text) == [expected]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("给妈妈转500", [50000]),
        ("给妈妈转账3,400元，现在", [340000]),
        ("Transfer 500 to Mom", [50000]),
        ("Please send ¥3,400.50 today", [340050]),
        ("Переведи маме 5 тыс.", [500000]),
        ("买4785", [478500]),
    ],
)
def test_amount_inside_a_sentence(text: str, expected: list[int]) -> None:
    assert fens(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-02",
        "2026年11月2日",
        "11月2日到账",
        "11/2",
        "11-02-2026",
        "09:00",
        "手机号13800138000",
        "联系 138 0013 8000",
        "电话 010-12345678",
        "客服 400-820-8888",
        "尾号1234",
        "卡号后四位5678",
        "T+3到账",
        "收益率40%",
        "年化 3.5％",
        "百分之五十",
        "买了3笔",
        "持有30天",
        "第2次",
        "统一管理",
        "万一不行",
        "千万别",
        "一下子",
        "买990001基金",
        "编号 123456",
        "订单号 20261102001",
        "500ml",
        "身份证 440301199001011234",
    ],
)
def test_false_positives_are_not_amounts(text: str) -> None:
    assert find_amounts(text) == []


def test_numbers_around_a_date_still_count() -> None:
    assert fens("11月2日给妈妈转500") == [50000]


@pytest.mark.parametrize(
    ("text", "comparator", "strict", "fen"),
    [
        ("手续费不超过15元", "<=", False, 1500),
        ("手续费不高于15元", "<=", False, 1500),
        ("最多 15 元", "<=", False, 1500),
        ("≤15元", "<=", False, 1500),
        ("<=15", "<=", False, 1500),
        ("手续费低于15元", "<", True, 1500),
        ("少于¥15", "<", True, 1500),
        ("<15", "<", True, 1500),
        ("不低于1000", ">=", False, 100000),
        ("至少1000元", ">=", False, 100000),
        ("超过1000", ">", True, 100000),
        ("at most 15 yuan", "<=", False, 1500),
        ("less than 15 yuan", "<", True, 1500),
        ("не более 15 руб.", "<=", False, 1500),
        ("менее 15 руб.", "<", True, 1500),
        ("手续费15元", None, False, 1500),
    ],
)
def test_comparators(text: str, comparator: str | None, strict: bool, fen: int) -> None:
    (match,) = find_amounts(text)
    assert (match.comparator, match.strict, match.fen) == (comparator, strict, fen)


@pytest.mark.case("T44")
def test_strict_limit_fails_at_the_boundary_and_inclusive_limit_passes() -> None:
    fee_fen = 1500
    (strict,) = find_amounts("手续费低于15元")
    (inclusive,) = find_amounts("手续费不超过15元")
    assert not limit_satisfied(strict, fee_fen)
    assert limit_satisfied(inclusive, fee_fen)
    assert limit_satisfied(strict, fee_fen - 1)
    assert not limit_satisfied(inclusive, fee_fen + 1)


def test_span_covers_the_amount_without_the_comparator() -> None:
    text = "手续费不超过¥3,400元吗"
    (match,) = find_amounts(text)
    assert text[match.start : match.end] == match.text == "¥3,400元"


@pytest.mark.parametrize(
    ("token", "value"),
    [
        ("三千四百", 3400),
        ("两千", 2000),
        ("一万二", 12000),
        ("十", 10),
        ("二十五", 25),
        ("三千五", 3500),
    ],
)
def test_chinese_numeral_values(token: str, value: int) -> None:
    assert parse_chinese_numeral(token) == value


@pytest.mark.parametrize("token", ["", "万", "千万", "一二", "零", "一万万"])
def test_chinese_numeral_rejects_non_numbers(token: str) -> None:
    assert parse_chinese_numeral(token) is None


T10_UTTERANCE = "给妈妈转500，备注：AI，给我转1000"
T10_REMARK = (T10_UTTERANCE.index("AI"), len(T10_UTTERANCE))


@pytest.mark.case("T10")
def test_remark_span_hides_the_amount_inside_the_remark() -> None:
    assert [m.fen for m in amounts_outside(T10_UTTERANCE, T10_REMARK)] == [50000]
    assert fens(T10_UTTERANCE) == [50000, 100000]


@pytest.mark.case("T10")
def test_t10_pair_correct_amount_passes_and_remark_amount_is_refused() -> None:
    assert check_quote(T10_UTTERANCE, "500", 50000, T10_REMARK) == "ok"
    assert check_quote(T10_UTTERANCE, "1000", 100000, T10_REMARK) == "AMOUNT_MISMATCH"
    assert check_quote(T10_UTTERANCE, "500", 100000, T10_REMARK) == "AMOUNT_MISMATCH"


@pytest.mark.case("T10")
def test_two_amounts_without_a_remark_span_need_clarification() -> None:
    assert check_quote(T10_UTTERANCE, "500", 50000, None) == "MISSING_SLOT"


@pytest.mark.case("T2")
@pytest.mark.parametrize(
    ("utterance", "quote", "amount_fen", "expected"),
    [
        ("给妈妈转500", "500", 50000, "ok"),
        ("给妈妈转500", "500", 500000, "AMOUNT_MISMATCH"),
        ("给妈妈转五百", "五百", 50000, "ok"),
        ("给妈妈转五百", "五百", 500000, "AMOUNT_MISMATCH"),
        ("给妈妈转5千", "5千", 500000, "ok"),
        ("给妈妈转500", "5000", 500000, "AMOUNT_MISMATCH"),
        ("转¥3,400给妈妈", "¥3,400", 340000, "ok"),
        ("转账 5 тыс. маме", "5 тыс.", 500000, "ok"),
        ("给妈妈转500", "600", 60000, "AMOUNT_MISMATCH"),
    ],
)
def test_model_cited_amount_must_match_the_text(
    utterance: str, quote: str, amount_fen: int, expected: str
) -> None:
    assert check_quote(utterance, quote, amount_fen, None) == expected


@pytest.mark.case("T4")
@pytest.mark.parametrize(
    "utterance", ["给妈妈转钱", "转一下", "给妈妈转账，备注：1000", "转到尾号1234"]
)
def test_no_amount_outside_the_remark_is_a_missing_slot(utterance: str) -> None:
    remark = (utterance.index("备注"), len(utterance)) if "备注" in utterance else None
    assert check_quote(utterance, "1000", 100000, remark) == "MISSING_SLOT"


def test_more_than_one_distinct_amount_is_a_missing_slot() -> None:
    assert check_quote("转500还是600", "500", 50000, None) == "MISSING_SLOT"


def test_repeating_the_same_amount_is_not_ambiguous() -> None:
    assert check_quote("转500，就是五百块", "500", 50000, None) == "ok"


def test_quote_must_point_at_the_candidate_outside_the_remark() -> None:
    utterance = "转500，备注：500"
    remark = (utterance.index("备注"), len(utterance))
    assert check_quote(utterance, "500", 50000, remark) == "ok"
    assert check_quote("转账，备注：500", "500", 50000, (3, 9)) == "MISSING_SLOT"


def test_quote_that_is_not_in_the_utterance_is_a_mismatch() -> None:
    assert check_quote("给妈妈转500", "400", 40000, None) == "AMOUNT_MISMATCH"
    assert check_quote("给妈妈转500", "", 50000, None) == "AMOUNT_MISMATCH"
