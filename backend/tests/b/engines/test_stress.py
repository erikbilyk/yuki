"""Stress losses before and after a trade (SPEC §14.5)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wg.contracts.engines import Stress, StressShock
from wg.numeric import stress

from ..fund_builders import position

SHOCK = StressShock(name="market", loss_pct={"000001": "20", "000002": "10"})
BEFORE = (position("000001", Decimal("1000")), position("000002", Decimal("1000")))


def test_conforms_to_the_engine_protocol() -> None:
    engine: Stress = stress
    assert engine is stress


def test_loss_is_value_times_percent_and_after_adds_the_fee() -> None:
    after = (position("000001", Decimal("500")), position("000002", Decimal("1495")))
    (row,) = stress((SHOCK,), BEFORE, after, 500).rows
    # Before 200 + 100 yuan; after 100 + 149.50 + 5.00 fee.
    assert (row.shock, row.before_loss_fen, row.after_loss_fen) == ("market", 30000, 25450)
    assert row.change_fen == -4550


def test_cash_outside_the_funds_is_not_shocked() -> None:
    after = (position("000002", Decimal("1000")),)
    (row,) = stress((SHOCK,), BEFORE, after, 0).rows
    assert row.after_loss_fen == 10000


def test_rows_follow_the_order_of_the_shocks() -> None:
    calm = StressShock(name="calm", loss_pct={"000001": "0", "000002": "0"})
    result = stress((calm, SHOCK), BEFORE, BEFORE, 1500)
    assert [r.shock for r in result.rows] == ["calm", "market"]
    assert [r.change_fen for r in result.rows] == [1500, 1500]


def test_loss_rounds_once_half_up() -> None:
    # 3.33 yuan at 33.3333 % is 1.10999889 yuan; 0.03 yuan at 50 % is 0.015 yuan.
    shock = StressShock(name="s", loss_pct={"000001": "33.3333", "000002": "50"})
    holdings = (position("000001", Decimal("3.33")),)
    assert stress((shock,), holdings, holdings, 0).rows[0].before_loss_fen == 111
    holdings = (position("000002", Decimal("0.03")),)
    assert stress((shock,), holdings, holdings, 0).rows[0].before_loss_fen == 2


@pytest.mark.parametrize(
    "shock",
    [
        StressShock(name="partial", loss_pct={"000001": "20"}),
        StressShock(name="gain", loss_pct={"000001": "20", "000002": "-5"}),
    ],
    ids=["fund missing from the shock", "negative loss"],
)
def test_incomplete_or_negative_shock_is_refused(shock: StressShock) -> None:
    with pytest.raises(ValueError):
        stress((shock,), BEFORE, BEFORE, 0)


def test_negative_fee_is_refused() -> None:
    with pytest.raises(ValueError):
        stress((SHOCK,), BEFORE, BEFORE, -1)
