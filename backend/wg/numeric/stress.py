"""Losses under fixed shocks before and after a trade (SPEC §14.5)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from wg.contracts.engines import PositionValue, StressResult, StressRow, StressShock
from wg.contracts.money import quantize_fen


def stress(
    shocks: Sequence[StressShock],
    before: Sequence[PositionValue],
    after: Sequence[PositionValue],
    fee_fen: int,
) -> StressResult:
    """Loss of each shock on the holdings before and after the trade; the after loss adds the fee.

    Every held fund needs a loss percent in every shock; cash outside the funds is not shocked.
    """
    if fee_fen < 0:
        raise ValueError("fee must not be negative")
    rows = []
    for shock in shocks:
        before_loss = _loss_fen(shock, before)
        after_loss = _loss_fen(shock, after) + fee_fen
        rows.append(
            StressRow(
                shock=shock.name,
                before_loss_fen=before_loss,
                after_loss_fen=after_loss,
                change_fen=after_loss - before_loss,
            )
        )
    return StressResult(fee_fen=fee_fen, rows=tuple(rows))


def _loss_fen(shock: StressShock, positions: Sequence[PositionValue]) -> int:
    loss = Decimal(0)
    for position in positions:
        pct = shock.loss_pct.get(position.product_id)
        if pct is None:
            raise ValueError(f"shock {shock.name} has no loss for {position.product_id}")
        if pct < 0:
            raise ValueError(f"shock {shock.name} has a negative loss for {position.product_id}")
        loss += position.value_fen * pct
    # value_fen × percent is in hundredths of a fen; /10000 gives yuan.
    return quantize_fen(loss / 10000)
