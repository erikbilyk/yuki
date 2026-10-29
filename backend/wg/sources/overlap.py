"""Overlap of two funds' stock holdings."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from wg.sources.holdings import Holding

_SIX_PLACES = Decimal("0.000001")


@dataclass(frozen=True)
class OverlapRow:
    security_code: str
    security_name: str
    value_fen_a: int
    value_fen_b: int
    weight_a: Decimal
    weight_b: Decimal
    shared_weight: Decimal
    pages_a: tuple[int, ...]
    pages_b: tuple[int, ...]


def _by_code(rows: list[Holding]) -> dict[str, list[Holding]]:
    grouped: dict[str, list[Holding]] = defaultdict(list)
    for row in rows:
        grouped[row.security_code].append(row)
    return grouped


def overlap_rows(
    rows_a: list[Holding], net_assets_a_fen: int, rows_b: list[Holding], net_assets_b_fen: int
) -> list[OverlapRow]:
    """Per shared security: the smaller of the two fund-level weights.

    A security held in both the index and the active sleeve of one fund is summed first.
    """
    grouped_a, grouped_b = _by_code(rows_a), _by_code(rows_b)
    result = []
    for code in sorted(grouped_a.keys() & grouped_b.keys()):
        value_a = sum(row.value_fen for row in grouped_a[code])
        value_b = sum(row.value_fen for row in grouped_b[code])
        weight_a = Decimal(value_a) / Decimal(net_assets_a_fen)
        weight_b = Decimal(value_b) / Decimal(net_assets_b_fen)
        result.append(
            OverlapRow(
                security_code=code,
                security_name=grouped_a[code][0].security_name,
                value_fen_a=value_a,
                value_fen_b=value_b,
                weight_a=weight_a,
                weight_b=weight_b,
                shared_weight=min(weight_a, weight_b),
                pages_a=tuple(sorted({row.page for row in grouped_a[code]})),
                pages_b=tuple(sorted({row.page for row in grouped_b[code]})),
            )
        )
    return result


def overlap_percent(rows: list[OverlapRow]) -> Decimal:
    """Sum of shared weights as a percent, rounded half up to six decimals."""
    return (sum((row.shared_weight for row in rows), Decimal(0)) * 100).quantize(
        _SIX_PLACES, rounding=ROUND_HALF_UP
    )
