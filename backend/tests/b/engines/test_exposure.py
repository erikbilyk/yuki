"""Exposure bounds, target status and the executable flag (SPEC §14.2)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from wg.contracts.engines import (
    Exposure,
    ExposureGroup,
    HoldingRow,
    HoldingsSnapshot,
    TargetStatus,
)
from wg.numeric import exposure

from ..fund_builders import position

AS_OF = date(2026, 10, 31)


def snapshot(
    code: str,
    tech: str,
    *,
    unresolved: str = "0.0000",
    executable: bool = True,
    as_of: date = AS_OF,
    taxonomy: str = "syn_taxonomy_v1",
) -> HoldingsSnapshot:
    """A fund with `tech` weight in technology (issuer T) and the rest disclosed elsewhere."""
    rest = Decimal(1) - Decimal(tech) - Decimal(unresolved)
    return HoldingsSnapshot(
        product_id=code,
        as_of=as_of,
        source_id="test",
        executable=executable,
        taxonomy_version=taxonomy,
        unresolved_weight=unresolved,
        rows=(
            HoldingRow(security_code="T", issuer="发行人T", sector="technology", weight=tech),
            HoldingRow(security_code="F", issuer="发行人F", sector="finance", weight=rest),
        ),
    )


def tech(positions, snapshots, target: str | None = "40"):
    target_pct = None if target is None else Decimal(target)
    return exposure(positions, snapshots, ExposureGroup.SECTOR, "technology", target_pct)


def test_conforms_to_the_engine_protocol() -> None:
    engine: Exposure = exposure
    assert engine is exposure


def test_lower_and_upper_with_unresolved_weight() -> None:
    positions = (position("000001", Decimal("6000")), position("000002", Decimal("4000")))
    snapshots = (snapshot("000001", "0.3000", unresolved="0.1000"), snapshot("000002", "0.5000"))
    result = tech(positions, snapshots, target=None)
    # lower = (6000·0.3 + 4000·0.5) / 10000; upper adds 6000·0.1.
    assert (result.lower_fen, result.upper_fen) == (380000, 440000)
    assert (str(result.lower_pct), str(result.upper_pct)) == ("38.0000", "44.0000")
    assert str(result.unresolved_pct) == "6.0000"
    assert result.status is None and result.target_max_pct is None


@pytest.mark.parametrize(
    ("target", "status"),
    [
        ("37.9999", TargetStatus.NOT_MET),
        ("38.0000", TargetStatus.UNVERIFIED),
        ("43.9999", TargetStatus.UNVERIFIED),
        ("44.0000", TargetStatus.MET),
    ],
)
def test_status_against_the_bounds(target: str, status: TargetStatus) -> None:
    positions = (position("000001", Decimal("6000")), position("000002", Decimal("4000")))
    snapshots = (snapshot("000001", "0.3000", unresolved="0.1000"), snapshot("000002", "0.5000"))
    assert tech(positions, snapshots, target).status is status


def test_status_compares_exact_values_not_the_rounded_percent() -> None:
    snaps = (snapshot("000001", "1.0000"), snapshot("000002", "0.4000"))
    at_target = (position("000002", Decimal("10000")),)
    assert tech(at_target, snaps).status is TargetStatus.MET
    # 10,000,000.6 / 25,000,000 fen is 40.0000024 %: it prints as 40.0000 and still fails.
    over = (position("000001", Decimal("0.0100")), position("000002", Decimal("249999.9900")))
    result = tech(over, snaps)
    assert str(result.lower_pct) == "40.0000"
    assert result.status is TargetStatus.NOT_MET


def test_position_without_snapshot_is_wholly_unresolved_and_not_executable() -> None:
    positions = (position("000001", Decimal("5000")), position("000002", Decimal("5000")))
    result = tech(positions, (snapshot("000001", "0.2000"),))
    assert (str(result.lower_pct), str(result.upper_pct)) == ("10.0000", "60.0000")
    assert result.status is TargetStatus.UNVERIFIED
    assert not result.executable


def test_position_with_no_value_needs_no_snapshot() -> None:
    positions = (position("000001", Decimal("5000")), position("000002", Decimal("0")))
    result = tech(positions, (snapshot("000001", "0.2000"),))
    assert result.executable
    assert result.status is TargetStatus.MET


def test_public_snapshot_is_dated_diagnostics_only() -> None:
    positions = (position("000001", Decimal("5000")), position("000002", Decimal("5000")))
    public = snapshot("000002", "0.1000", executable=False, as_of=date(2026, 6, 30))
    result = tech(positions, (snapshot("000001", "0.2000"), public))
    assert result.status is TargetStatus.MET
    assert not result.executable
    assert result.holdings_as_of == date(2026, 6, 30)


def test_issuer_group_uses_issuer_rows() -> None:
    positions = (position("000001", Decimal("5000")),)
    result = exposure(
        positions, (snapshot("000001", "0.2000"),), ExposureGroup.ISSUER, "发行人F", None
    )
    assert str(result.lower_pct) == "80.0000"
    assert result.key == "发行人F"
    assert result.group is ExposureGroup.ISSUER


def test_unknown_key_has_zero_exposure() -> None:
    positions = (position("000001", Decimal("5000")),)
    result = exposure(positions, (snapshot("000001", "0.2000"),), ExposureGroup.SECTOR, "x", None)
    assert (result.lower_fen, result.upper_fen) == (0, 0)


def test_fen_bounds_round_half_up() -> None:
    # 0.0333 of 1.50 yuan is 0.049950 yuan: 4.995 fen rounds to 5.
    positions = (position("000001", Decimal("1.5000")),)
    result = tech(positions, (snapshot("000001", "0.0333"),), target=None)
    assert result.lower_fen == 5


@pytest.mark.parametrize(
    "snapshots",
    [
        (snapshot("000001", "0.2"), snapshot("000001", "0.3")),
        (snapshot("000001", "0.2"), snapshot("000002", "0.2", taxonomy="other_v1")),
        (),
    ],
    ids=["duplicate snapshot", "mixed taxonomies", "no snapshot"],
)
def test_inconsistent_snapshots_are_refused(snapshots) -> None:
    positions = (position("000001", Decimal("5000")), position("000002", Decimal("5000")))
    with pytest.raises(ValueError):
        tech(positions, snapshots)


def test_empty_portfolio_is_refused() -> None:
    with pytest.raises(ValueError):
        tech((), (snapshot("000001", "0.2"),))
