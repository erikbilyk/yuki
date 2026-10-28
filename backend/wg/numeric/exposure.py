"""Sector and issuer exposure of fund positions, with lower and upper bounds (SPEC §14.2)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from wg.contracts.engines import (
    ExposureGroup,
    ExposureResult,
    HoldingRow,
    HoldingsSnapshot,
    PositionValue,
    TargetStatus,
)
from wg.contracts.money import quantize_fen

HUNDRED = Decimal(100)
FOUR_PLACES = Decimal("0.0001")


def exposure(
    positions: Sequence[PositionValue],
    snapshots: Sequence[HoldingsSnapshot],
    group: ExposureGroup,
    key: str,
    target_max_pct: Decimal | None,
) -> ExposureResult:
    """Exposure of the positions to one sector or issuer.

    A position without a holdings snapshot counts as wholly unresolved: it adds to the upper
    bound only, and the result is not executable.
    """
    by_product = _snapshots_by_product(snapshots)
    total = sum(p.value_fen for p in positions)
    if total <= 0:
        raise ValueError("exposure needs positions with a positive total value")

    lower = Decimal(0)
    unresolved = Decimal(0)
    used: list[HoldingsSnapshot] = []
    complete = True
    for position in positions:
        if position.value_fen == 0:
            continue
        snapshot = by_product.get(position.product_id)
        if snapshot is None:
            unresolved += position.value_fen
            complete = False
            continue
        used.append(snapshot)
        lower += position.value_fen * disclosed_weight(snapshot, group, key)
        unresolved += position.value_fen * snapshot.unresolved_weight
    if not used:
        raise ValueError("no holdings snapshot covers the positions")

    upper = lower + unresolved
    return ExposureResult(
        group=group,
        key=key,
        total_fen=total,
        lower_fen=quantize_fen(lower / HUNDRED),
        upper_fen=quantize_fen(upper / HUNDRED),
        lower_pct=_pct(lower, total),
        upper_pct=_pct(upper, total),
        target_max_pct=target_max_pct,
        status=None if target_max_pct is None else _status(lower, upper, total, target_max_pct),
        holdings_as_of=min(s.as_of for s in used),
        taxonomy_version=_taxonomy(used),
        unresolved_pct=_pct(unresolved, total),
        executable=complete and all(s.executable for s in used),
    )


def _snapshots_by_product(snapshots: Sequence[HoldingsSnapshot]) -> dict[str, HoldingsSnapshot]:
    by_product: dict[str, HoldingsSnapshot] = {}
    for snapshot in snapshots:
        if snapshot.product_id in by_product:
            raise ValueError(f"two holdings snapshots for {snapshot.product_id}")
        by_product[snapshot.product_id] = snapshot
    return by_product


def disclosed_weight(snapshot: HoldingsSnapshot, group: ExposureGroup, key: str) -> Decimal:
    """Sum of the disclosed weights of one fund in the sector or issuer `key`."""
    return sum((row.weight for row in snapshot.rows if _row_key(row, group) == key), Decimal(0))


def _row_key(row: HoldingRow, group: ExposureGroup) -> str:
    return row.sector if group is ExposureGroup.SECTOR else row.issuer


def _status(lower: Decimal, upper: Decimal, total: int, target_pct: Decimal) -> TargetStatus:
    # Compared on exact values: a share that rounds to the target but exceeds it fails.
    limit = target_pct * total
    if upper * HUNDRED <= limit:
        return TargetStatus.MET
    if lower * HUNDRED > limit:
        return TargetStatus.NOT_MET
    return TargetStatus.UNVERIFIED


def _taxonomy(snapshots: Sequence[HoldingsSnapshot]) -> str:
    versions = {s.taxonomy_version for s in snapshots}
    if len(versions) != 1:
        raise ValueError(f"holdings snapshots mix taxonomies: {sorted(versions)}")
    return versions.pop()


def _pct(part: Decimal, total: int) -> Decimal:
    return (part * HUNDRED / total).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
