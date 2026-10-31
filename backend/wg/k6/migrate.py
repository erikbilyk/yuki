"""Apply the SQL migrations in wg/k6/schema/ in order; each one runs in its own unit of work.

A migration whose first line is `-- foreign_keys: off` runs with foreign key enforcement
off, so it can rebuild a table that other tables reference; it commits only when
`PRAGMA foreign_key_check` finds no violation.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from wg.k6.db import Database, Reader, UnitOfWork
from wg.k6.simtime import wall_now

log = logging.getLogger("wg.k6")

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

_FILE_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

FK_OFF_LINE = "-- foreign_keys: off"


class MigrationError(RuntimeError):
    """A migration left the database inconsistent and was rolled back."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def script(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def foreign_keys_off(self) -> bool:
        return self.script.split("\n", 1)[0].rstrip() == FK_OFF_LINE


def discover(directory: Path = SCHEMA_DIR) -> list[Migration]:
    """Migrations sorted by version; a gap or a duplicate version is an error."""
    found = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILE_NAME.match(path.name)
        if match is None:
            raise ValueError(f"migration file name must be NNNN_name.sql: {path.name}")
        found.append(Migration(int(match[1]), match[2], path))
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise ValueError(f"migration versions must be 1..n without gaps: {versions}")
    return found


def split_statements(script: str) -> list[str]:
    """Split a script into complete statements, keeping trigger bodies whole."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        if not buffer and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise ValueError("migration ends with an incomplete statement")
    return statements


def current_version(reader: Reader) -> int:
    exists = reader.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if exists is None:
        return 0
    row = reader.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def _ensure_version_table(uow: UnitOfWork) -> None:
    uow.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )


def _apply(uow: UnitOfWork, migration: Migration) -> None:
    for statement in split_statements(migration.script):
        uow.execute(statement)
    if migration.foreign_keys_off:
        violations = uow.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            tables = sorted({row[0] for row in violations})
            raise MigrationError(
                f"migration {migration.version:04d} breaks foreign keys in {', '.join(tables)}"
            )
    # applied_at is wall-clock time: the simulated clock lives in a table this creates.
    uow.execute(
        "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
        (migration.version, migration.name, wall_now()),
    )


def migrate(db: Database, directory: Path = SCHEMA_DIR) -> int:
    """Bring the database to the latest version and return it; running it again is a no-op."""
    migrations = discover(directory)
    with db.unit_of_work("schema_version") as uow:
        _ensure_version_table(uow)
    for migration in migrations:
        unit = (
            db.unit_of_work_without_foreign_keys if migration.foreign_keys_off else db.unit_of_work
        )
        with unit(f"migrate {migration.version:04d}") as uow:
            if current_version(uow) >= migration.version:
                continue
            _apply(uow, migration)
            log.info("migrated version=%d name=%s", migration.version, migration.name)
    with db.read() as conn:
        return current_version(conn)
