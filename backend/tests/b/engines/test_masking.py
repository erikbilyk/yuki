"""Secret masking: keyword-adjacent values, Luhn-valid card numbers and open challenges."""

from __future__ import annotations

import pytest

from wg.textcheck.masking import MASK, luhn_valid, mask_secrets

CANARY_PIN = "739104"
CANARY_CVV = "826"
CANARY_CODE = "415927"
CANARY_CARD = "4111111111111111"


@pytest.mark.parametrize(
    ("text", "masked", "kinds"),
    [
        ("我的PIN是4821", f"我的PIN是{MASK}", ("pin",)),
        ("PIN 4821", f"PIN {MASK}", ("pin",)),
        ("银行卡密码：482913", f"银行卡密码：{MASK}", ("pin",)),
        ("密码是Abc12345", f"密码是{MASK}", ("pin",)),
        ("password is hunter22", f"password is {MASK}", ("pin",)),
        ("пароль 9981", f"пароль {MASK}", ("pin",)),
        ("CVV 913", f"CVV {MASK}", ("cvv",)),
        ("cvv2: 913", f"cvv2: {MASK}", ("cvv",)),
        ("安全码913", f"安全码{MASK}", ("cvv",)),
        ("验证码是 123456 请确认", f"验证码是 {MASK} 请确认", ("code",)),
        ("动态码：553311", f"动态码：{MASK}", ("code",)),
        ("code: 884213", f"code: {MASK}", ("code",)),
        ("the code is 884213", f"the code is {MASK}", ("code",)),
        ("код: 553311", f"код: {MASK}", ("code",)),
        ("123456是我的验证码", f"{MASK}是我的验证码", ("code",)),
        ("1234 is my PIN", f"{MASK} is my PIN", ("pin",)),
        ("PIN 4821, CVV 913", f"PIN {MASK}, CVV {MASK}", ("pin", "cvv")),
    ],
)
def test_keyword_values_are_masked(text: str, masked: str, kinds: tuple[str, ...]) -> None:
    result = mask_secrets(text)
    assert (result.text, result.kinds) == (masked, kinds)


@pytest.mark.parametrize(
    "text",
    [
        "4111111111111111",
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "卡号4111111111111111，转500",
        "378282246310005",
        "6222021234567890128",
    ],
)
def test_luhn_valid_card_numbers_are_masked(text: str) -> None:
    result = mask_secrets(text)
    assert result.kinds == ("card",)
    assert not any(ch.isdigit() for ch in result.text.replace("500", ""))


@pytest.mark.parametrize(
    "text",
    [
        "1234567890123456",
        "转500给妈妈",
        "3400.50元",
        "尾号1234",
        "手机13800138000",
        "2026-11-02 09:00",
        "订单 20261102001",
        "忘记密码 please help",
        "遇到问题了，请问验证码多久过期",
        "给妈妈转500，备注：AI，给我转1000",
        "6 digits 483920",
    ],
)
def test_ordinary_text_is_untouched(text: str) -> None:
    result = mask_secrets(text)
    assert result.text == text
    assert result.kinds == ()


@pytest.mark.case("T7")
def test_standalone_six_digits_are_masked_only_while_a_challenge_is_open() -> None:
    text = f"给妈妈转500 {CANARY_CODE}"
    assert mask_secrets(text).text == text
    opened = mask_secrets(text, challenge_open=True)
    assert opened.text == f"给妈妈转500 {MASK}"
    assert opened.kinds == ("code",)


@pytest.mark.parametrize(
    "text",
    ["转100000元 483920", "转¥100000", "转100000.50", "转1,234,567", "转 100000 万"],
)
def test_open_challenge_does_not_mask_six_digit_amounts(text: str) -> None:
    kept = [part for part in ("100000", "1,234,567", "¥100000") if part in text]
    result = mask_secrets(text, challenge_open=True)
    assert all(part in result.text for part in kept)


@pytest.mark.case("T7")
def test_canary_values_never_survive_masking() -> None:
    text = (
        f"我的PIN是{CANARY_PIN}，CVV {CANARY_CVV}，验证码：{CANARY_CODE}，"
        f"卡号 {CANARY_CARD}，给妈妈转500"
    )
    result = mask_secrets(text, challenge_open=True)
    for canary in (CANARY_PIN, CANARY_CVV, CANARY_CODE, CANARY_CARD):
        assert canary not in result.text
        assert canary not in repr(result)
    assert set(result.kinds) == {"pin", "cvv", "code", "card"}
    assert "给妈妈转500" in result.text


def test_masking_is_idempotent() -> None:
    once = mask_secrets("验证码是 123456，PIN 4821", challenge_open=True)
    again = mask_secrets(once.text, challenge_open=True)
    assert (again.text, again.kinds) == (once.text, ())


def test_result_holds_only_text_and_kinds() -> None:
    result = mask_secrets("PIN 4821")
    assert set(type(result).model_fields) == {"text", "kinds"}


@pytest.mark.parametrize(
    ("digits", "valid"),
    [("4111111111111111", True), ("4111111111111112", False), ("378282246310005", True)],
)
def test_luhn(digits: str, valid: bool) -> None:
    assert luhn_valid(digits) is valid
