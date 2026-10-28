from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from wg.contracts.money import DecimalStr, Fen, Pct, Shares, percent_str, quantize_fen

SHARES = TypeAdapter(Shares)
FEN = TypeAdapter(Fen)


@pytest.mark.parametrize(
    "value",
    [
        1.5,
        5000.0,
        "NaN",
        "nan",
        "Infinity",
        "-Infinity",
        "inf",
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        "1e3",
        "1E3",
        "1e-3",
        "1.5e2",
        "",
        " 1",
        "1 ",
        "+1",
        ".5",
        "5.",
        "1,000",
        "٣",
        5000,
        True,
        None,
    ],
)
def test_decimal_str_rejects(value):
    with pytest.raises(ValidationError):
        SHARES.validate_python(value)


def test_decimal_str_rejects_json_numbers():
    for raw in ("5000", "5000.0", "1e3", "NaN"):
        with pytest.raises(ValidationError):
            SHARES.validate_json(raw)


def test_decimal_str_rejects_more_places_than_declared():
    with pytest.raises(ValidationError):
        SHARES.validate_python("1.23456")
    with pytest.raises(ValidationError):
        SHARES.validate_python(Decimal("1.00001"))
    with pytest.raises(ValidationError):
        TypeAdapter(DecimalStr(2)).validate_python("0.125")


def test_decimal_str_normalises_to_declared_places():
    assert str(SHARES.validate_python("5000")) == "5000.0000"
    assert str(SHARES.validate_python(Decimal("39.5"))) == "39.5000"
    assert str(SHARES.validate_python("-0.25")) == "-0.2500"
    assert str(TypeAdapter(DecimalStr(0)).validate_python("12")) == "12"


def test_decimal_str_serialises_as_json_string():
    value = TypeAdapter(Pct).validate_json('"39.5089"')
    assert value == Decimal("39.5089")
    assert TypeAdapter(Pct).dump_json(value) == b'"39.5089"'
    assert TypeAdapter(Pct).dump_python(value, mode="json") == "39.5089"
    assert TypeAdapter(Pct).json_schema()["type"] == "string"


@pytest.mark.parametrize("value", ["5", 5.0, True, None])
def test_fen_is_a_strict_int(value):
    with pytest.raises(ValidationError):
        FEN.validate_python(value)
    assert FEN.validate_python(498500) == 498500


@pytest.mark.parametrize(
    ("yuan", "fen"),
    [
        ("0.005", 1),
        ("0.0049", 0),
        ("0.015", 2),
        ("0.0149", 1),
        ("4985", 498500),
        ("27.50", 2750),
        ("19748.50", 1974850),
        ("-0.005", -1),
        ("-0.0049", 0),
    ],
)
def test_quantize_fen_rounds_half_up(yuan, fen):
    assert quantize_fen(Decimal(yuan)) == fen


@pytest.mark.parametrize("value", [0.005, 1, Decimal("NaN"), Decimal("Infinity")])
def test_quantize_fen_needs_a_finite_decimal(value):
    with pytest.raises(TypeError):
        quantize_fen(value)


def test_percent_str_rounds_half_up():
    assert percent_str(Decimal("39.508902")) == "39.5089"
    assert percent_str(Decimal("39.50885")) == "39.5089"
    assert percent_str(Decimal("39.5089"), places=1) == "39.5"
    assert percent_str(Decimal("39.55"), places=1) == "39.6"
    assert percent_str(Decimal("45.5")) == "45.5000"
    with pytest.raises(TypeError):
        percent_str(39.5)
