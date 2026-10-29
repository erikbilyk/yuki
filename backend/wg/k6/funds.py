"""Fund products, positions, lots and holdings snapshots (SPEC §7, §14).

Positions and lots change only when a fund order settles (`FundsPack.on_settled`). Lots are
drawn oldest first, and the shares of a fund's lots add up to its position whenever lots
are recorded at all; a position without lots is a fund whose lots are unknown (T43), which
leaves its redemption fee unverified.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from wg.contracts.engines import (
    FeeSchedule,
    FeeTier,
    FundProduct,
    HoldingRow,
    HoldingsSnapshot,
    Lot,
    PositionValue,
)
from wg.contracts.enums import ActionKind, OperationStatus
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.fixture import FixturePlanConstants
from wg.contracts.ids import IdPrefix, new_id
from wg.contracts.money import quantize_fen
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import now_db

FOUR_PLACES = Decimal("0.0001")


def shares_text(shares: Decimal) -> str:
    """Stored form of a share count: exactly four decimals."""
    return str(shares.quantize(FOUR_PLACES))


@dataclass(frozen=True)
class ProductRow:
    product_id: str
    name: str
    share_class: str
    nav: Decimal
    nav_date: date
    fee_schedule: FeeSchedule
    min_purchase_fen: int
    redeem_settle_days: int
    purchase_settle_days: int

    def engine_product(self) -> FundProduct:
        return FundProduct(
            product_id=self.product_id,
            nav=self.nav,
            min_purchase_fen=self.min_purchase_fen,
            redeem_settle_days=self.redeem_settle_days,
            purchase_settle_days=self.purchase_settle_days,
            fee_schedule=self.fee_schedule,
        )


@dataclass(frozen=True)
class PositionRow:
    account_id: str
    product_id: str
    shares: Decimal
    version: int


def _schedule(reader: Reader, fee_schedule_id: str) -> FeeSchedule:
    row = reader.execute(
        "SELECT * FROM fee_schedules WHERE fee_schedule_id = ?", (fee_schedule_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no fee schedule {fee_schedule_id}")
    return FeeSchedule(
        schedule_id=row["fee_schedule_id"],
        terms_version=row["terms_version"],
        purchase_rate=Decimal(row["purchase_rate"]),
        tiers=tuple(FeeTier.model_validate(tier) for tier in json.loads(row["tiers_json"])),
    )


def _product(reader: Reader, row) -> ProductRow:
    return ProductRow(
        product_id=row["product_id"],
        name=row["name"],
        share_class=row["share_class"],
        nav=Decimal(row["nav"]),
        nav_date=date.fromisoformat(row["nav_date"][:10]),
        fee_schedule=_schedule(reader, row["fee_schedule_id"]),
        min_purchase_fen=row["min_purchase_fen"],
        redeem_settle_days=row["redeem_settle_days"],
        purchase_settle_days=row["purchase_settle_days"],
    )


def find_product(reader: Reader, product_id: str) -> ProductRow | None:
    row = reader.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
    return None if row is None else _product(reader, row)


def get_product(reader: Reader, product_id: str) -> ProductRow:
    product = find_product(reader, product_id)
    if product is None:
        raise LookupError(f"no product {product_id}")
    return product


def all_products(reader: Reader) -> list[ProductRow]:
    rows = reader.execute("SELECT * FROM products ORDER BY product_id").fetchall()
    return [_product(reader, row) for row in rows]


# Positions


def _position(row) -> PositionRow:
    return PositionRow(
        account_id=row["account_id"],
        product_id=row["product_id"],
        shares=Decimal(row["shares"]),
        version=row["version"],
    )


def customer_positions(reader: Reader, customer_id: str) -> list[PositionRow]:
    rows = reader.execute(
        "SELECT p.* FROM positions p JOIN accounts a ON a.account_id = p.account_id"
        " WHERE a.customer_id = ? ORDER BY p.product_id, p.account_id",
        (customer_id,),
    ).fetchall()
    return [_position(row) for row in rows]


def find_position(reader: Reader, account_id: str, product_id: str) -> PositionRow | None:
    row = reader.execute(
        "SELECT * FROM positions WHERE account_id = ? AND product_id = ?",
        (account_id, product_id),
    ).fetchone()
    return None if row is None else _position(row)


def position_values(reader: Reader, customer_id: str) -> list[PositionValue]:
    """The customer's held funds valued at their current NAV (gross, before any fee)."""
    values = []
    for position in customer_positions(reader, customer_id):
        if position.shares <= 0:
            continue
        product = get_product(reader, position.product_id)
        values.append(
            PositionValue(
                product_id=position.product_id,
                shares=position.shares,
                nav=product.nav,
                value_fen=quantize_fen(position.shares * product.nav),
            )
        )
    return values


def pending_redemption_shares(reader: Reader, account_id: str, product_id: str) -> Decimal:
    """Shares in redemptions submitted but not yet settled; they are no longer free."""
    rows = reader.execute(
        "SELECT shares FROM operations WHERE kind = ? AND account_id = ? AND product_id = ?"
        " AND status IN (?, ?)",
        (
            ActionKind.REDEEM.value,
            account_id,
            product_id,
            OperationStatus.SUBMITTED.value,
            OperationStatus.UNKNOWN.value,
        ),
    ).fetchall()
    return sum((Decimal(row["shares"]) for row in rows), Decimal(0))


def add_shares(uow: UnitOfWork, account_id: str, product_id: str, delta: Decimal) -> PositionRow:
    """Change a position by `delta` shares; a purchase of a fund not held opens a position."""
    position = find_position(uow, account_id, product_id)
    if position is None:
        if delta < 0:
            raise WgError(
                ErrorCode.INTEGRITY_VIOLATION,
                {"reason": "position_missing", "product_id": product_id},
            )
        uow.execute(
            "INSERT INTO positions (account_id, product_id, shares, created_at)"
            " VALUES (?, ?, ?, ?)",
            (account_id, product_id, shares_text(delta), now_db(uow)),
        )
    else:
        shares = position.shares + delta
        if shares < 0:
            raise WgError(
                ErrorCode.INTEGRITY_VIOLATION,
                {"reason": "negative_position", "product_id": product_id},
            )
        cursor = uow.execute(
            "UPDATE positions SET shares = ?, version = version + 1"
            " WHERE account_id = ? AND product_id = ? AND version = ?",
            (shares_text(shares), account_id, product_id, position.version),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"position {account_id}/{product_id} changed since it was read")
    updated = find_position(uow, account_id, product_id)
    assert updated is not None
    return updated


# Lots


def lots_of(reader: Reader, account_id: str, product_id: str) -> list[Lot]:
    """The lots of one position, oldest first (the order a redemption draws them)."""
    rows = reader.execute(
        "SELECT * FROM lots WHERE account_id = ? AND product_id = ? ORDER BY acquired_on, lot_id",
        (account_id, product_id),
    ).fetchall()
    return [
        Lot(
            lot_id=row["lot_id"],
            product_id=row["product_id"],
            shares=Decimal(row["shares"]),
            acquired_on=date.fromisoformat(row["acquired_on"][:10]),
            cost_fen=row["cost_fen"],
        )
        for row in rows
    ]


def customer_lots(reader: Reader, customer_id: str) -> list[Lot]:
    lots = []
    for position in customer_positions(reader, customer_id):
        lots.extend(lots_of(reader, position.account_id, position.product_id))
    return lots


def check_lots_match(reader: Reader, account_id: str, product_id: str) -> None:
    """Recorded lots add up to the position; a mismatch is a data invariant (SPEC §16)."""
    lots = lots_of(reader, account_id, product_id)
    if not lots:
        return
    position = find_position(reader, account_id, product_id)
    held = position.shares if position is not None else Decimal(0)
    if sum((lot.shares for lot in lots), Decimal(0)) != held:
        raise WgError(
            ErrorCode.INTEGRITY_VIOLATION,
            {"reason": "lots_mismatch", "account_id": account_id, "product_id": product_id},
        )


def draw_lots(uow: UnitOfWork, account_id: str, product_id: str, shares: Decimal) -> None:
    """Take redeemed shares from the oldest lots; an emptied lot is removed.

    A position without recorded lots has nothing to draw from.
    """
    lots = lots_of(uow, account_id, product_id)
    if not lots:
        return
    remaining = shares
    for lot in lots:
        if remaining <= 0:
            break
        taken = min(lot.shares, remaining)
        if taken == lot.shares:
            uow.execute("DELETE FROM lots WHERE lot_id = ?", (lot.lot_id,))
        else:
            uow.execute(
                "UPDATE lots SET shares = ?, version = version + 1 WHERE lot_id = ?",
                (shares_text(lot.shares - taken), lot.lot_id),
            )
        remaining -= taken
    if remaining > 0:
        raise WgError(
            ErrorCode.INTEGRITY_VIOLATION,
            {"reason": "lots_mismatch", "account_id": account_id, "product_id": product_id},
        )


def insert_lot(
    uow: UnitOfWork,
    *,
    account_id: str,
    product_id: str,
    shares: Decimal,
    acquired_on: date,
    cost_fen: int,
) -> str:
    """A new lot for bought shares, acquired on the settlement date."""
    lot_id = new_id(IdPrefix.LOT)
    uow.execute(
        "INSERT INTO lots (lot_id, account_id, product_id, shares, acquired_on, cost_fen,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            lot_id,
            account_id,
            product_id,
            shares_text(shares),
            acquired_on.isoformat(),
            cost_fen,
            now_db(uow),
        ),
    )
    return lot_id


# Holdings snapshots


def holdings_snapshots(reader: Reader, *, executable: bool) -> list[HoldingsSnapshot]:
    """The newest snapshot of each fund with the given `executable` flag."""
    rows = reader.execute(
        "SELECT * FROM holdings_snapshots h WHERE h.executable = ? AND h.as_of = ("
        " SELECT MAX(as_of) FROM holdings_snapshots"
        " WHERE product_id = h.product_id AND executable = h.executable)"
        " ORDER BY h.product_id, h.source_id, h.security_code",
        (int(executable),),
    ).fetchall()
    grouped: dict[tuple[str, str], list] = {}
    for row in rows:
        grouped.setdefault((row["product_id"], row["source_id"]), []).append(row)
    snapshots = []
    for (product_id, source_id), group in grouped.items():
        first = group[0]
        snapshots.append(
            HoldingsSnapshot(
                product_id=product_id,
                as_of=date.fromisoformat(first["as_of"][:10]),
                source_id=source_id,
                executable=bool(first["executable"]),
                taxonomy_version=first["taxonomy_version"],
                unresolved_weight=Decimal(first["unresolved_weight"]),
                rows=tuple(
                    HoldingRow(
                        security_code=row["security_code"],
                        issuer=row["issuer"],
                        sector=row["sector_code"],
                        weight=Decimal(row["weight"]),
                    )
                    for row in group
                ),
            )
        )
    return snapshots


# Plan totals


def plan_trades(reader: Reader, plan_id: str) -> list[tuple[int, int]]:
    """(gross_fen, fee_fen) of every fund order of the plan that the bank did not reject.

    The bank's record holds the terms each order was accepted at, so a limit counts the
    plan's trades cumulatively (SPEC §12.3 step 4).
    """
    rows = reader.execute(
        "SELECT b.effect_json FROM operations o"
        " JOIN actions a ON a.logical_action_id = o.logical_action_id AND a.revision = o.revision"
        " JOIN bank_requests b ON b.idempotency_key = o.idempotency_key"
        " WHERE a.plan_id = ? AND o.kind IN (?, ?) AND o.status <> ? AND b.status <> 'REJECTED'",
        (
            plan_id,
            ActionKind.REDEEM.value,
            ActionKind.PURCHASE.value,
            OperationStatus.REJECTED.value,
        ),
    ).fetchall()
    trades = []
    for row in rows:
        effect = json.loads(row["effect_json"])
        trades.append((int(effect["gross_fen"]), int(effect["fee_fen"])))
    return trades


def plan_constants(reader: Reader, customer_id: str) -> FixturePlanConstants:
    """The customer's rebalance defaults: sector, target, checkpoint, caps, expiry, bounds."""
    row = reader.execute(
        "SELECT plan_constants_json FROM customer_settings WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no settings for {customer_id}")
    return FixturePlanConstants.model_validate_json(row["plan_constants_json"])
