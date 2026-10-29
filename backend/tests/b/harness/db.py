"""Read-only view of the database for facts no route exposes (tables and columns of SPEC §7).

Every query is a SELECT on a `mode=ro` connection; tests never change the database directly.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

TABLES = frozenset(
    {
        "actions",
        "approvals",
        "audit_log",
        "bank_requests",
        "calc_results",
        "cards",
        "cash_snapshots",
        "challenges",
        "checkpoint_results",
        "commitment_events",
        "commitments",
        "domain_events",
        "event_cursors",
        "failure_records",
        "import_batches",
        "ledger_entries",
        "locks",
        "operation_events",
        "operations",
        "plan_revisions",
        "plans",
        "positions",
        "receipts",
        "reservations",
        "reserves",
        "sim_clock",
        "sms_outbox",
        "transactions",
    }
)


class DbView:
    def __init__(self, path: Path) -> None:
        self.path = path

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("DbView runs SELECT statements only")
        uri = f"file:{self.path}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def count(self, table: str) -> int:
        if table not in TABLES:
            raise ValueError(f"unknown table {table}")
        return self.rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]

    def operations(self, logical_action_id: str | None = None) -> list[dict[str, Any]]:
        if logical_action_id is None:
            return self.rows("SELECT * FROM operations ORDER BY submitted_at")
        return self.rows(
            "SELECT * FROM operations WHERE logical_action_id = ? ORDER BY revision",
            (logical_action_id,),
        )

    def money_operations(self, customer_id: str) -> list[dict[str, Any]]:
        """TRANSFER and PAY_BILL operations of a customer (the daily-sum kinds, SPEC §10.2)."""
        return self.rows(
            "SELECT * FROM operations WHERE customer_id = ? AND kind IN ('TRANSFER', 'PAY_BILL')",
            (customer_id,),
        )

    def ledger_entries(self, operation_id: str) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM ledger_entries WHERE operation_id = ?", (operation_id,))

    def receipts(self, operation_id: str) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM receipts WHERE operation_id = ?", (operation_id,))

    def actions(self, logical_action_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM actions WHERE logical_action_id = ? ORDER BY revision",
            (logical_action_id,),
        )

    def action_status(self, logical_action_id: str) -> str:
        """Status of the newest revision of a logical action."""
        rows = self.actions(logical_action_id)
        if not rows:
            raise AssertionError(f"no action {logical_action_id}")
        return rows[-1]["status"]

    def reservations(self, state: str | None = None) -> list[dict[str, Any]]:
        if state is None:
            return self.rows("SELECT * FROM reservations")
        return self.rows("SELECT * FROM reservations WHERE state = ?", (state,))

    def challenges(self, logical_action_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM challenges WHERE logical_action_id = ? ORDER BY issued_at",
            (logical_action_id,),
        )

    def sms_codes(self, challenge_id: str) -> list[Any]:
        rows = self.rows("SELECT code FROM sms_outbox WHERE challenge_id = ?", (challenge_id,))
        return [row["code"] for row in rows]

    def clock(self) -> dict[str, Any]:
        return self.rows("SELECT * FROM sim_clock")[0]

    def commitment(self, commitment_id: str) -> dict[str, Any]:
        rows = self.rows("SELECT * FROM commitments WHERE commitment_id = ?", (commitment_id,))
        if not rows:
            raise AssertionError(f"no commitment {commitment_id}")
        return rows[0]

    def commitments(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM commitments ORDER BY commitment_id")

    def commitment_events(self, commitment_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM commitment_events WHERE commitment_id = ? ORDER BY commitment_event_id",
            (commitment_id,),
        )

    def reserve_target(self, customer_id: str) -> int:
        rows = self.rows(
            "SELECT target_fen FROM reserves WHERE customer_id = ? AND kind = 'emergency'",
            (customer_id,),
        )
        return rows[0]["target_fen"]

    def snapshot_hash(self, snapshot_id: str) -> str:
        rows = self.rows(
            "SELECT inputs_hash FROM cash_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        )
        return rows[0]["inputs_hash"]

    def position_shares(self, product_id: str) -> str:
        """Shares of a product in the customer's single position, as stored text."""
        rows = self.rows("SELECT shares FROM positions WHERE product_id = ?", (product_id,))
        assert len(rows) == 1, f"{len(rows)} positions for {product_id}"
        return rows[0]["shares"]

    def fund_operations(self, kind: str) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM operations WHERE kind = ? ORDER BY submitted_at", (kind,))

    def checkpoints(self, plan_id: str) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM checkpoint_results WHERE plan_id = ?", (plan_id,))

    def events(self, event_type: str) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM domain_events WHERE type = ? ORDER BY seq", (event_type,))

    def operation_events(self, operation_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM operation_events WHERE operation_id = ? ORDER BY operation_event_id",
            (operation_id,),
        )

    def bank_requests(self, idempotency_key: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM bank_requests WHERE idempotency_key = ?", (idempotency_key,)
        )

    def failure_records(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM failure_records ORDER BY failure_id")

    def audit_rows(self, actor: str | None = None) -> list[dict[str, Any]]:
        if actor is None:
            return self.rows("SELECT * FROM audit_log ORDER BY audit_id")
        return self.rows("SELECT * FROM audit_log WHERE actor = ? ORDER BY audit_id", (actor,))

    def plan_actions(self, plan_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM actions WHERE plan_id = ? ORDER BY logical_action_id, revision",
            (plan_id,),
        )

    def authority_counts(self) -> dict[str, int]:
        """Approvals, challenges and grants (verified challenges) of every customer."""
        row = self.rows(
            "SELECT (SELECT COUNT(*) FROM approvals) AS approvals,"
            " (SELECT COUNT(*) FROM challenges) AS challenges,"
            " (SELECT COUNT(*) FROM challenges WHERE verified_at IS NOT NULL) AS grants"
        )[0]
        return dict(row)
