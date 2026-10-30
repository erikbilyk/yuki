"""Read-only queries on the episode database: what the customer's screen would show.

The queries never write. Card terms and action statuses are read from the records the
gateway itself uses, so the customer policy does not depend on the model's wording.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any


@contextmanager
def _reader(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


class DbView:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _rows(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with _reader(self.path) as conn:
            return conn.execute(sql, args).fetchall()

    def open_cards(self, customer_id: str) -> list[dict[str, Any]]:
        """Cards that no approval has confirmed yet, oldest first, with their terms."""
        rows = self._rows(
            "SELECT card_id, kind, plan_id, logical_action_id, revision, terms_json, terms_hash"
            " FROM cards WHERE customer_id = ? AND approval_id IS NULL ORDER BY created_at, rowid",
            customer_id,
        )
        return [{**dict(row), "terms": json.loads(row["terms_json"])} for row in rows]

    def action_params(self, logical_action_id: str) -> dict[str, Any] | None:
        """Kind, status and params of the active revision of an action."""
        rows = self._rows(
            "SELECT kind, status, params_json FROM actions"
            " WHERE logical_action_id = ? AND status <> 'SUPERSEDED'",
            logical_action_id,
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "kind": row["kind"],
            "status": row["status"],
            "params": json.loads(row["params_json"]),
        }

    def actions_awaiting_stepup(self, customer_id: str) -> list[str]:
        rows = self._rows(
            "SELECT logical_action_id FROM actions"
            " WHERE customer_id = ? AND status = 'AWAITING_STEPUP' ORDER BY created_at, rowid",
            customer_id,
        )
        return [row["logical_action_id"] for row in rows]

    def newest_action_status(self, customer_id: str, kind: str) -> str | None:
        rows = self._rows(
            "SELECT status FROM actions WHERE customer_id = ? AND kind = ?"
            " AND status <> 'SUPERSEDED' ORDER BY created_at DESC, rowid DESC LIMIT 1",
            customer_id,
            kind,
        )
        return rows[0]["status"] if rows else None

    def newest_plan_status(self, customer_id: str) -> str | None:
        rows = self._rows(
            "SELECT status FROM plans WHERE customer_id = ? ORDER BY created_at DESC, rowid DESC"
            " LIMIT 1",
            customer_id,
        )
        return rows[0]["status"] if rows else None

    def operation_ids(self, customer_id: str) -> list[str]:
        rows = self._rows(
            "SELECT operation_id FROM operations WHERE customer_id = ?"
            " AND logical_action_id IS NOT NULL ORDER BY submitted_at, rowid",
            customer_id,
        )
        return [row["operation_id"] for row in rows]

    def audit_mark(self) -> int:
        return self._rows("SELECT COALESCE(MAX(audit_id), 0) AS mark FROM audit_log")[0]["mark"]

    def audit_since(self, mark: int) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT audit_id, at, actor, event, ref, decision, reason FROM audit_log"
            " WHERE audit_id > ? ORDER BY audit_id",
            mark,
        )
        return [dict(row) for row in rows]


def copy_database(source: Path, target: Path) -> None:
    """A single-file copy of a database that no process writes any more, with its log folded in.

    After a SIGKILL the newest rows may exist only in the `-wal` file. The source is
    checkpointed first, the copy is taken through SQLite (which reads the log), and the copy
    leaves WAL mode, so it needs no sidecar file when the scorer opens it.
    """
    with closing(sqlite3.connect(source, timeout=10)) as src:
        try:
            src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass  # busy: the backup below still reads the log
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")


def copy_file(source: Path, target: Path) -> None:
    if source.is_file():
        shutil.copyfile(source, target)
