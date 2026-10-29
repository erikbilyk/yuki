"""Approval cards and approvals (SPEC §9.2).

A card stores the terms the trusted UI showed and their hash. An approval is created from a
card when the customer confirms it and is immutable afterwards, except `revoked_at`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from wg.contracts.api import CardKind
from wg.contracts.enums import ApprovalScopeKind, Tier
from wg.k6.actions import params_json
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, now_db, to_db


@dataclass(frozen=True)
class CardRow:
    card_id: str
    customer_id: str
    kind: CardKind
    plan_id: str | None
    logical_action_id: str | None
    revision: int | None
    parent_approval_id: str | None
    tier: Tier
    terms: Mapping[str, Any]
    terms_hash: str
    content: Mapping[str, Any]
    cash_snapshot_id: str | None
    calc_result_id: str | None
    expires_at: datetime
    approval_id: str | None
    created_at: datetime


@dataclass(frozen=True)
class ApprovalRow:
    approval_id: str
    customer_id: str
    scope_kind: ApprovalScopeKind
    plan_id: str | None
    logical_action_id: str | None
    revision: int | None
    terms: Mapping[str, Any]
    terms_hash: str
    bounds: Mapping[str, Any] | None
    parent_approval_id: str | None
    cash_snapshot_id: str | None
    calc_result_id: str | None
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime


def _card(row) -> CardRow:
    return CardRow(
        card_id=row["card_id"],
        customer_id=row["customer_id"],
        kind=CardKind(row["kind"]),
        plan_id=row["plan_id"],
        logical_action_id=row["logical_action_id"],
        revision=row["revision"],
        parent_approval_id=row["parent_approval_id"],
        tier=Tier(row["tier"]),
        terms=json.loads(row["terms_json"]),
        terms_hash=row["terms_hash"],
        content=json.loads(row["content_json"]),
        cash_snapshot_id=row["cash_snapshot_id"],
        calc_result_id=row["calc_result_id"],
        expires_at=from_db(row["expires_at"]),
        approval_id=row["approval_id"],
        created_at=from_db(row["created_at"]),
    )


def _approval(row) -> ApprovalRow:
    return ApprovalRow(
        approval_id=row["approval_id"],
        customer_id=row["customer_id"],
        scope_kind=ApprovalScopeKind(row["scope_kind"]),
        plan_id=row["plan_id"],
        logical_action_id=row["logical_action_id"],
        revision=row["revision"],
        terms=json.loads(row["terms_json"]),
        terms_hash=row["terms_hash"],
        bounds=json.loads(row["bounds_json"]) if row["bounds_json"] else None,
        parent_approval_id=row["parent_approval_id"],
        cash_snapshot_id=row["cash_snapshot_id"],
        calc_result_id=row["calc_result_id"],
        expires_at=from_db(row["expires_at"]),
        revoked_at=from_db(row["revoked_at"]) if row["revoked_at"] else None,
        created_at=from_db(row["created_at"]),
    )


# Cards


def insert_card(
    uow: UnitOfWork,
    *,
    card_id: str,
    customer_id: str,
    kind: CardKind,
    tier: Tier,
    terms: Mapping[str, Any],
    terms_hash: str,
    content: Mapping[str, Any],
    expires_at: datetime,
    plan_id: str | None = None,
    logical_action_id: str | None = None,
    revision: int | None = None,
    parent_approval_id: str | None = None,
    cash_snapshot_id: str | None = None,
    calc_result_id: str | None = None,
) -> CardRow:
    uow.execute(
        "INSERT INTO cards (card_id, customer_id, kind, plan_id, logical_action_id, revision,"
        " parent_approval_id, tier, terms_json, terms_hash, content_json, cash_snapshot_id,"
        " calc_result_id, expires_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            card_id,
            customer_id,
            CardKind(kind).value,
            plan_id,
            logical_action_id,
            revision,
            parent_approval_id,
            Tier(tier).value,
            params_json(terms),
            terms_hash,
            params_json(content),
            cash_snapshot_id,
            calc_result_id,
            to_db(expires_at),
            now_db(uow),
        ),
    )
    card = find_card(uow, card_id)
    assert card is not None
    return card


def find_card(reader: Reader, card_id: str) -> CardRow | None:
    row = reader.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
    return None if row is None else _card(row)


def cards_of_action(reader: Reader, logical_action_id: str, revision: int) -> list[CardRow]:
    rows = reader.execute(
        "SELECT * FROM cards WHERE logical_action_id = ? AND revision = ? ORDER BY rowid",
        (logical_action_id, revision),
    ).fetchall()
    return [_card(row) for row in rows]


def set_card_approval(uow: UnitOfWork, card_id: str, approval_id: str) -> None:
    """Record the approval created from this card; a card is confirmed once."""
    cursor = uow.execute(
        "UPDATE cards SET approval_id = ? WHERE card_id = ? AND approval_id IS NULL",
        (approval_id, card_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"card {card_id} is already confirmed")


# Approvals


def insert_approval(
    uow: UnitOfWork,
    *,
    approval_id: str,
    customer_id: str,
    scope_kind: ApprovalScopeKind,
    terms: Mapping[str, Any],
    terms_hash: str,
    expires_at: datetime,
    plan_id: str | None = None,
    logical_action_id: str | None = None,
    revision: int | None = None,
    bounds: Mapping[str, Any] | None = None,
    parent_approval_id: str | None = None,
    cash_snapshot_id: str | None = None,
    calc_result_id: str | None = None,
) -> ApprovalRow:
    uow.execute(
        "INSERT INTO approvals (approval_id, customer_id, scope_kind, plan_id,"
        " logical_action_id, revision, terms_json, terms_hash, bounds_json, parent_approval_id,"
        " cash_snapshot_id, calc_result_id, expires_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            approval_id,
            customer_id,
            ApprovalScopeKind(scope_kind).value,
            plan_id,
            logical_action_id,
            revision,
            params_json(terms),
            terms_hash,
            params_json(bounds) if bounds is not None else None,
            parent_approval_id,
            cash_snapshot_id,
            calc_result_id,
            to_db(expires_at),
            now_db(uow),
        ),
    )
    approval = find_approval(uow, approval_id)
    assert approval is not None
    return approval


def find_approval(reader: Reader, approval_id: str) -> ApprovalRow | None:
    row = reader.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
    return None if row is None else _approval(row)


def approvals_of_action(reader: Reader, logical_action_id: str, revision: int) -> list[ApprovalRow]:
    """Action-scope approvals of one revision, newest first."""
    rows = reader.execute(
        "SELECT * FROM approvals WHERE scope_kind = 'action'"
        " AND logical_action_id = ? AND revision = ? ORDER BY rowid DESC",
        (logical_action_id, revision),
    ).fetchall()
    return [_approval(row) for row in rows]


def approvals_of_plan(reader: Reader, plan_id: str) -> list[ApprovalRow]:
    """Plan-scope approvals of a plan, newest first."""
    rows = reader.execute(
        "SELECT * FROM approvals WHERE scope_kind = 'plan' AND plan_id = ? ORDER BY rowid DESC",
        (plan_id,),
    ).fetchall()
    return [_approval(row) for row in rows]


def revoke_approval(uow: UnitOfWork, approval_id: str, at: datetime) -> bool:
    """Set `revoked_at` once; False when the approval was already revoked."""
    cursor = uow.execute(
        "UPDATE approvals SET revoked_at = ? WHERE approval_id = ? AND revoked_at IS NULL",
        (to_db(at), approval_id),
    )
    return cursor.rowcount == 1


def cards_of_plan(reader: Reader, plan_id: str) -> list[CardRow]:
    """Plan and plan-revision cards of a plan, oldest first."""
    rows = reader.execute(
        "SELECT * FROM cards WHERE plan_id = ? AND kind <> 'action' ORDER BY rowid", (plan_id,)
    ).fetchall()
    return [_card(row) for row in rows]
