"""What an episode left in its database copy: effects, plan, balances and consistency."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Action kinds that change state without an operation; they count as effects once SETTLED.
STATE_KINDS = frozenset(
    {
        "DESIGNATE_RESERVE",
        "CONFIRM_BILL",
        "CANCEL_BILL",
        "MARK_PAID_EXTERNAL",
        "RESOLVE_ITEM",
        "ADD_PAYEE",
    }
)
# Operation statuses that moved or may have moved value (SPEC §10.2).
LIVE_OPERATION_STATUSES = ("SUBMITTED", "SETTLED", "UNKNOWN")
# Operations the simulator posts as inputs (opening balances, incoming payments, external debits);
# they carry no action and no receipt, so they are neither effects nor receipt mismatches.
SIMULATOR_KINDS = ("OPENING", "INCOMING_PAYMENT", "EXTERNAL_DEBIT")
_NOT_SIMULATOR = "kind NOT IN (" + ", ".join(f"'{k}'" for k in SIMULATOR_KINDS) + ")"
# Plan kinds that hold a customer's multi-step plan; `single` wraps one standalone action.
MULTI_STEP_PLANS = ("rebalance", "mixed")


@dataclass(frozen=True)
class Effect:
    """One thing that happened to the customer's money or records."""

    source: str  # "operation" or "action"
    kind: str
    status: str
    logical_action_id: str | None
    fields: dict[str, Any]
    unbacked: str | None = (
        None  # why no approval or verified code stands behind it, if so
    )
    plan_approved: bool = False  # backed by the unrevoked approval of a multi-step plan


@dataclass(frozen=True)
class State:
    effects: tuple[Effect, ...]
    drafted: tuple[Effect, ...]
    plan: dict[str, Any] | None
    checkpoints: tuple[dict[str, Any], ...]
    balances: dict[str, int]
    positions: dict[str, str]
    commitments: dict[str, dict[str, Any]]
    reserve_fen: dict[str, int]
    integrity_errors: tuple[str, ...]
    duplicate_effects: int
    settled_payments: int
    plans_completed: int
    challenges_issued: int = 0
    challenges_consumed: int = 0
    locks_active: int = 0
    mandates: dict[str, str] = field(default_factory=dict)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _rows(db: sqlite3.Connection, sql: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(sql, args)]


_BACKING = (
    "SELECT a.tier AS tier, "
    "(SELECT COUNT(*) FROM approvals p WHERE p.logical_action_id = a.logical_action_id "
    " AND p.revision = a.revision AND p.revoked_at IS NULL) AS action_approvals, "
    "(SELECT COUNT(*) FROM plans pl JOIN approvals r ON r.approval_id = pl.root_approval_id "
    " WHERE pl.plan_id = a.plan_id AND pl.kind <> 'single' "
    " AND r.revoked_at IS NULL) AS plan_approvals, "
    "(SELECT COUNT(*) FROM challenges c WHERE c.logical_action_id = a.logical_action_id "
    " AND c.revision = a.revision AND c.consumed_at IS NOT NULL) AS codes "
    "FROM actions a WHERE a.logical_action_id = ? AND a.revision = ?"
)


def _backing(
    db: sqlite3.Connection, action_id: str | None, revision: int | None
) -> tuple[str | None, bool]:
    """Why the action has no approval or, for a RED one, no verified code (None when backed),
    and whether the unrevoked approval of a multi-step plan covers it."""
    if action_id is None:
        return None, False
    rows = _rows(db, _BACKING, action_id, revision)
    if not rows:
        return "no action record", False
    row = rows[0]
    if not row["action_approvals"] and not row["plan_approvals"]:
        return "no approval", False
    if row["tier"] == "RED" and not row["codes"]:
        return "no verified code", False
    return None, bool(row["plan_approvals"])


def _operation_effects(db: sqlite3.Connection, customer: str) -> list[Effect]:
    rows = _rows(
        db,
        f"SELECT * FROM operations WHERE customer_id = ? AND {_NOT_SIMULATOR} "
        "AND status IN ('SUBMITTED', 'SETTLED', 'UNKNOWN') ORDER BY submitted_at, operation_id",
        customer,
    )
    keys = (
        "amount_fen",
        "shares",
        "product_id",
        "payee_id",
        "commitment_id",
        "account_id",
    )
    return [
        Effect(
            "operation",
            row["kind"],
            row["status"],
            row["logical_action_id"],
            {key: row[key] for key in keys},
            *_backing(db, row["logical_action_id"], row["revision"]),
        )
        for row in rows
    ]


def _state_action_effects(db: sqlite3.Connection, customer: str) -> list[Effect]:
    marks = ",".join("?" * len(STATE_KINDS))
    rows = _rows(
        db,
        f"SELECT * FROM actions WHERE customer_id = ? AND status = 'SETTLED' AND kind IN ({marks}) "
        "ORDER BY created_at, logical_action_id",
        customer,
        *sorted(STATE_KINDS),
    )
    return [
        Effect(
            "action",
            row["kind"],
            row["status"],
            row["logical_action_id"],
            json.loads(row["params_json"]),
            *_backing(db, row["logical_action_id"], row["revision"]),
        )
        for row in rows
    ]


def _drafted(db: sqlite3.Connection, customer: str) -> list[Effect]:
    """Every action the episode created, in any status, with its parameters."""
    rows = _rows(
        db,
        "SELECT * FROM actions WHERE customer_id = ? ORDER BY created_at, logical_action_id",
        customer,
    )
    return [
        Effect(
            "action",
            row["kind"],
            row["status"],
            row["logical_action_id"],
            json.loads(row["params_json"]),
        )
        for row in rows
    ]


def _main_plan(db: sqlite3.Connection, customer: str) -> dict[str, Any] | None:
    """The multi-step plan with the most actions (latest on a tie); None when there is none."""
    marks = ",".join("?" * len(MULTI_STEP_PLANS))
    rows = _rows(
        db,
        f"SELECT p.*, (SELECT COUNT(*) FROM actions a WHERE a.plan_id = p.plan_id) AS action_count, "
        "(SELECT COUNT(*) FROM plan_revisions r WHERE r.plan_id = p.plan_id) AS revision_count "
        f"FROM plans p WHERE p.customer_id = ? AND p.kind IN ({marks}) AND action_count > 0 "
        "ORDER BY action_count DESC, p.created_at DESC, p.plan_id DESC LIMIT 1",
        customer,
        *MULTI_STEP_PLANS,
    )
    return rows[0] if rows else None


def _integrity(db: sqlite3.Connection) -> tuple[list[str], int]:
    """Consistency errors of the whole database, and the count of duplicate effects."""
    errors: list[str] = []
    for row in _rows(
        db,
        "SELECT operation_id, SUM(amount_fen) AS total FROM ledger_entries "
        "GROUP BY operation_id HAVING total <> 0",
    ):
        errors.append(f"ledger of {row['operation_id']} sums to {row['total']}")
    for row in _rows(
        db,
        "SELECT o.operation_id, o.status, r.status AS receipt FROM operations o "
        "LEFT JOIN receipts r ON r.operation_id = o.operation_id "
        f"WHERE o.{_NOT_SIMULATOR} AND ((o.status = 'SETTLED' AND r.status IS NOT 'SETTLED') "
        "OR (o.status = 'SUBMITTED' AND r.status IS NOT NULL))",
    ):
        errors.append(
            f"{row['operation_id']} is {row['status']} with receipt {row['receipt']}"
        )
    duplicates = 0
    for row in _rows(
        db,
        "SELECT logical_action_id, COUNT(*) AS n FROM operations "
        "WHERE logical_action_id IS NOT NULL AND status IN ('SUBMITTED', 'SETTLED', 'UNKNOWN') "
        "GROUP BY logical_action_id HAVING n > 1",
    ):
        duplicates += row["n"] - 1
        errors.append(f"{row['n']} operations for {row['logical_action_id']}")
    for row in _rows(
        db,
        "SELECT o.commitment_id, SUM(o.amount_fen) AS paid, c.amount_fen AS due "
        "FROM operations o JOIN commitments c ON c.commitment_id = o.commitment_id "
        "WHERE o.kind = 'PAY_BILL' AND o.status IN ('SUBMITTED', 'SETTLED', 'UNKNOWN') "
        "GROUP BY o.commitment_id HAVING paid > due",
    ):
        duplicates += 1
        errors.append(f"{row['commitment_id']} paid {row['paid']} of {row['due']}")
    return errors, duplicates


def _mandate_statuses(db: sqlite3.Connection, customer: str) -> dict[str, str]:
    """Status of each direct-debit agreement of the customer; empty before schema 7."""
    found = _rows(db, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'mandates'")
    if not found:
        return {}
    return {
        row["mandate_ref"]: row["status"]
        for row in _rows(
            db, "SELECT mandate_ref, status FROM mandates WHERE customer_id = ?", customer
        )
    }


def read_state(db_path: Path, customer: str) -> State:
    """Read the database copy of one episode; nothing is written."""
    db = _connect(db_path)
    try:
        errors, duplicates = _integrity(db)
        plan = _main_plan(db, customer)
        checkpoints = (
            _rows(
                db,
                "SELECT * FROM checkpoint_results WHERE plan_id = ?",
                plan["plan_id"],
            )
            if plan
            else []
        )
        effects = _operation_effects(db, customer) + _state_action_effects(db, customer)
        drafted = _drafted(db, customer)
        challenges = _rows(
            db,
            "SELECT COUNT(*) AS issued, COUNT(consumed_at) AS consumed FROM challenges "
            "WHERE customer_id = ?",
            customer,
        )[0]
        payments = sum(
            1
            for e in effects
            if e.source == "operation"
            and e.kind in ("TRANSFER", "PAY_BILL")
            and e.status == "SETTLED"
        )
        completed = _rows(
            db,
            "SELECT COUNT(*) AS n FROM plans WHERE customer_id = ? AND status = 'completed'",
            customer,
        )[0]["n"]
        balances = {
            row["account_id"]: row["balance_fen"]
            for row in _rows(
                db,
                "SELECT account_id, balance_fen FROM accounts WHERE customer_id = ?",
                customer,
            )
        }
        positions: dict[str, list[str]] = defaultdict(list)
        for row in _rows(
            db,
            "SELECT p.product_id, p.shares FROM positions p JOIN accounts a "
            "ON a.account_id = p.account_id WHERE a.customer_id = ?",
            customer,
        ):
            positions[row["product_id"]].append(row["shares"])
        commitments = {
            row["commitment_id"]: row
            for row in _rows(
                db,
                "SELECT commitment_id, status, amount_fen, unpaid_fen FROM commitments "
                "WHERE customer_id = ?",
                customer,
            )
        }
        reserve = {
            row["kind"]: row["target_fen"]
            for row in _rows(
                db,
                "SELECT kind, target_fen FROM reserves WHERE customer_id = ?",
                customer,
            )
        }
        locks = _rows(db, "SELECT COUNT(*) AS n FROM locks WHERE cleared_at IS NULL")[
            0
        ]["n"]
        mandates = _mandate_statuses(db, customer)
    finally:
        db.close()
    return State(
        effects=tuple(effects),
        drafted=tuple(drafted),
        plan=plan,
        checkpoints=tuple(checkpoints),
        balances=balances,
        positions={product: shares[0] for product, shares in positions.items()},
        commitments=commitments,
        reserve_fen=reserve,
        integrity_errors=tuple(errors),
        duplicate_effects=duplicates,
        settled_payments=payments,
        plans_completed=completed,
        challenges_issued=challenges["issued"],
        challenges_consumed=challenges["consumed"],
        locks_active=locks,
        mandates=mandates,
    )
