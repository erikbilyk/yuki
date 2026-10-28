"""Builders that turn the fixture into funds engine inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from wg.contracts.engines import (
    CandidateSearchInput,
    FeeSchedule,
    FeeTier,
    FundProduct,
    HoldingRow,
    HoldingsSnapshot,
    Lot,
    PositionValue,
    StressShock,
)
from wg.contracts.money import quantize_fen

from .goldens.data import load_expected, load_fixture

LETTERS: dict[str, str] = load_expected()["products"]
CODES = {code: letter for letter, code in LETTERS.items()}


@dataclass(frozen=True)
class FundMarket:
    """Funds side of one fixture profile at the epoch."""

    epoch: date
    positions: tuple[PositionValue, ...]
    snapshots: tuple[HoldingsSnapshot, ...]
    products: tuple[FundProduct, ...]
    lots: tuple[Lot, ...]
    plan: dict[str, Any]

    def product(self, letter: str) -> FundProduct:
        return next(p for p in self.products if p.product_id == LETTERS[letter])

    def lots_of(self, letter: str) -> tuple[Lot, ...]:
        return tuple(lot for lot in self.lots if lot.product_id == LETTERS[letter])

    def search_input(self, headroom_fen: int = 200_000, **changes: Any) -> CandidateSearchInput:
        base = CandidateSearchInput(
            as_of=self.epoch,
            checkpoint_on=self.epoch + timedelta(days=self.plan["checkpoint_day"]),
            positions=self.positions,
            snapshots=self.snapshots,
            products=self.products,
            lots=self.lots,
            sector=self.plan["sector"],
            target_max_pct=self.plan["target_max_pct"],
            fee_cap_fen=self.plan["fee_cap_fen"],
            turnover_cap_fen=self.plan["turnover_cap_fen"],
            headroom_fen=headroom_fen,
            cash_snapshot_id="snp_epoch",
        )
        return changed(base, **changes)


def changed[M: BaseModel](model: M, **changes: Any) -> M:
    """A validated copy of `model` with some fields replaced (model_copy skips validation)."""
    return type(model).model_validate({**model.model_dump(), **changes})


def fixture_market(variant: str | None = None) -> FundMarket:
    doc = load_fixture(variant)
    epoch = datetime.fromisoformat(doc["epoch"]).date()
    schedules = {s["id"]: _schedule(s) for s in doc["fee_schedules"]}
    products = tuple(
        FundProduct(
            product_id=p["id"],
            nav=p["nav"],
            min_purchase_fen=p["min_purchase_fen"],
            redeem_settle_days=p["redeem_settle_days"],
            purchase_settle_days=p["purchase_settle_days"],
            fee_schedule=schedules[p["fee_schedule"]],
        )
        for p in doc["products"]
    )
    navs = {p.product_id: p.nav for p in products}
    positions = tuple(
        position(p["product"], Decimal(p["shares"]), navs[p["product"]]) for p in doc["positions"]
    )
    snapshots = tuple(
        HoldingsSnapshot(
            product_id=s["product"],
            as_of=date.fromisoformat(s["as_of"]),
            source_id=s["source_id"],
            executable=s["executable"],
            taxonomy_version=s["taxonomy_version"],
            unresolved_weight=s["unresolved_weight"],
            rows=tuple(HoldingRow(**row) for row in s["rows"]),
        )
        for s in doc["holdings_snapshots"]
    )
    lots = tuple(
        Lot(
            lot_id=lot["id"],
            product_id=lot["product"],
            shares=lot["shares"],
            acquired_on=epoch + timedelta(days=lot["acquired_offset_days"]),
            cost_fen=lot["cost_fen"],
        )
        for lot in doc["lots"]
    )
    return FundMarket(epoch, positions, snapshots, products, lots, doc["plan_constants"])


def position(product_id: str, shares: Decimal, nav: Decimal = Decimal("1.0000")) -> PositionValue:
    return PositionValue(
        product_id=product_id, shares=shares, nav=nav, value_fen=quantize_fen(shares * nav)
    )


def holdings(values_fen: dict[str, int]) -> tuple[PositionValue, ...]:
    """Positions at NAV 1.0000 from fund letters and values in fen."""
    return tuple(
        position(LETTERS[letter], Decimal(fen) / 100) for letter, fen in values_fen.items()
    )


def shocks() -> tuple[StressShock, ...]:
    table = load_expected()["funds"]["stress"]["shocks_pct"]
    return tuple(
        StressShock(name=name, loss_pct=dict(zip(LETTERS.values(), pcts, strict=True)))
        for name, pcts in table.items()
    )


def _schedule(raw: dict[str, Any]) -> FeeSchedule:
    return FeeSchedule(
        schedule_id=raw["id"],
        terms_version=raw["terms_version"],
        purchase_rate=raw["purchase_rate"],
        tiers=tuple(FeeTier(**tier) for tier in raw["tiers"]),
    )
