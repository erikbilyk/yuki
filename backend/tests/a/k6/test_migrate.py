import sqlite3
from pathlib import Path

import pytest

from wg.contracts.enums import AuditActor
from wg.contracts.errors import ErrorClass
from wg.contracts.events import ClockAdvanced, DomainEventType
from wg.k6 import audit, events, failures
from wg.k6.db import Database
from wg.k6.migrate import SCHEMA_DIR, current_version, discover, migrate, split_statements

from .helpers import EPOCH, add_customer

pytestmark = pytest.mark.layer("D")

SCHEMA_V1_TABLES = {
    # core
    "sim_clock", "sim_profile", "customers", "operators", "sessions", "accounts", "payees",
    "customer_settings", "plans", "actions", "approvals", "challenges", "sms_outbox",
    "operations", "operation_events", "reservations", "ledger_entries", "receipts",
    "bank_requests", "domain_events", "event_cursors", "audit_log", "locks", "breaker_state",
    "failure_records", "faults", "chat_messages", "read_scopes", "cards",
    # S2
    "import_batches", "categories", "transactions", "commitments", "commitment_events",
    "reserves", "allowances", "cash_snapshots",
    # S3
    "fee_schedules", "products", "positions", "lots", "holdings_snapshots", "calc_results",
    "plan_revisions", "checkpoint_results",
    # channel
    "im_bindings", "im_pairing_codes", "im_messages_seen",
    # S5
    "merchants", "mandates", "mandate_events", "debit_requests", "effect_verification_events",
}  # fmt: skip


def tables(db: Database) -> set[str]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {row[0] for row in rows}


def test_schema_v1_has_every_table(blank_db: Database):
    assert tables(blank_db) == SCHEMA_V1_TABLES | {"schema_version"}
    with blank_db.read() as conn:
        assert current_version(conn) == len(discover())


def test_migrations_are_idempotent(blank_db: Database):
    before = tables(blank_db)
    add_customer(blank_db)
    assert migrate(blank_db) == len(discover())
    assert migrate(blank_db) == len(discover())
    assert tables(blank_db) == before
    with blank_db.read() as conn:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_version")]
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
    assert versions == list(range(1, len(discover()) + 1))


def test_migrations_apply_on_an_older_version(tmp_path: Path):
    partial = tmp_path / "schema"
    partial.mkdir()
    first = discover()[0]
    (partial / first.path.name).write_text(first.path.read_text())
    db = Database(tmp_path / "wg.db")
    assert migrate(db, partial) == 1
    assert migrate(db) == len(discover())
    db.close()


def test_discover_refuses_gaps_and_bad_names(tmp_path: Path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="gaps"):
        discover(tmp_path)
    (tmp_path / "0003_c.sql").rename(tmp_path / "two.sql")
    with pytest.raises(ValueError, match="NNNN_name"):
        discover(tmp_path)


def test_split_statements_keeps_trigger_bodies():
    script = (
        "-- comment\nCREATE TABLE t (x);\n\nCREATE TRIGGER g BEFORE UPDATE ON t\n"
        "BEGIN\n    SELECT RAISE(ABORT, 'no; never');\nEND;\n"
    )
    statements = split_statements(script)
    assert len(statements) == 2
    assert statements[1].endswith("END;")
    with pytest.raises(ValueError, match="incomplete"):
        split_statements("CREATE TABLE t (x)")


def test_every_migration_file_is_in_the_schema_dir():
    assert [m.path.parent for m in discover()] == [SCHEMA_DIR] * len(discover())


# UNIQUE constraints and invariants of SPEC §7, checked through a unit of work.


def must_fail(db: Database, *statements: str, match: str = "") -> None:
    with pytest.raises(sqlite3.IntegrityError, match=match):
        with db.unit_of_work("constraint") as uow:
            for statement in statements:
                uow.execute(statement)


def run(db: Database, *statements: str) -> None:
    with db.unit_of_work("setup") as uow:
        for statement in statements:
            uow.execute(statement)


ACTION = (
    "INSERT INTO actions (logical_action_id, revision, customer_id, kind, status, params_json,"
    " request_hash, tier, superseded_by, created_at) VALUES ('act_1', {rev}, 'cus_main',"
    " 'TRANSFER', '{status}', '{{}}', 'h', 'YELLOW', {sup}, 't')"
)
OPERATION = (
    "INSERT INTO operations (operation_id, idempotency_key, request_hash, kind, status,"
    " submitted_at, created_at) VALUES ('{op}', '{key}', 'h', 'TRANSFER', 'SUBMITTED', 't', 't')"
)


def test_one_active_revision_per_logical_action(blank_db: Database):
    add_customer(blank_db)
    run(blank_db, ACTION.format(rev=1, status="DRAFT", sup="NULL"))
    must_fail(blank_db, ACTION.format(rev=2, status="DRAFT", sup="NULL"), match="UNIQUE")
    run(
        blank_db,
        "UPDATE actions SET status = 'SUPERSEDED', superseded_by = 2",
        ACTION.format(rev=2, status="DRAFT", sup="NULL"),
    )
    must_fail(blank_db, ACTION.format(rev=3, status="READY", sup="NULL"), match="UNIQUE")


def test_superseded_needs_its_successor(blank_db: Database):
    add_customer(blank_db)
    must_fail(blank_db, ACTION.format(rev=1, status="SUPERSEDED", sup="NULL"), match="CHECK")


def test_one_operation_per_idempotency_key(blank_db: Database):
    run(blank_db, OPERATION.format(op="opn_1", key="act_1:1"))
    must_fail(blank_db, OPERATION.format(op="opn_2", key="act_1:1"), match="UNIQUE")


def test_one_receipt_per_operation_and_receipts_are_immutable(blank_db: Database):
    run(
        blank_db,
        OPERATION.format(op="opn_1", key="k"),
        "INSERT INTO receipts (receipt_id, operation_id, status, effect_json, created_at)"
        " VALUES ('rcp_1', 'opn_1', 'SETTLED', '{}', 't')",
    )
    must_fail(
        blank_db,
        "INSERT INTO receipts (receipt_id, operation_id, status, effect_json, created_at)"
        " VALUES ('rcp_2', 'opn_1', 'REJECTED', '{}', 't')",
        match="UNIQUE",
    )
    must_fail(blank_db, "UPDATE receipts SET status = 'REJECTED'", match="immutable")


def test_transactions_unique_per_account_and_source_id(blank_db: Database):
    add_customer(blank_db)
    run(
        blank_db,
        "INSERT INTO accounts (account_id, customer_id, kind, name, balance_fen, covered,"
        " created_at) VALUES ('acc_1', 'cus_main', 'customer', 'a', 0, 1, 't')",
        "INSERT INTO import_batches (batch_id, account_id, statement_id, period, opening_fen,"
        " closing_fen, new_count, duplicate_count, created_at)"
        " VALUES (1, 'acc_1', 'stm_1', 'p', 0, 0, 1, 0, 't')",
    )
    line = (
        "INSERT INTO transactions (account_id, source_txn_id, batch_id, posted_on, amount_fen,"
        " counterparty_raw, description_raw, created_at)"
        " VALUES ('acc_1', 'MAIN-1', 1, '2026-11-01', -100, 'c', 'd', 't')"
    )
    run(blank_db, line)
    must_fail(blank_db, line, match="UNIQUE")


def test_customer_accounts_cannot_go_negative(blank_db: Database):
    add_customer(blank_db)
    must_fail(
        blank_db,
        "INSERT INTO accounts (account_id, customer_id, kind, name, balance_fen, covered,"
        " created_at) VALUES ('acc_1', 'cus_main', 'customer', 'a', -1, 1, 't')",
        match="CHECK",
    )


def test_approvals_change_only_by_revocation_once(blank_db: Database):
    add_customer(blank_db)
    run(
        blank_db,
        "INSERT INTO approvals (approval_id, customer_id, scope_kind, logical_action_id,"
        " revision, terms_json, terms_hash, expires_at, created_at)"
        " VALUES ('apr_1', 'cus_main', 'action', 'act_1', 1, '{}', 'h', 'e', 't')",
    )
    must_fail(blank_db, "UPDATE approvals SET terms_hash = 'other'", match="immutable")
    run(blank_db, "UPDATE approvals SET revoked_at = 'now'")
    must_fail(blank_db, "UPDATE approvals SET revoked_at = 'later'", match="immutable")


def test_reservation_is_consumed_or_released_once(blank_db: Database):
    add_customer(blank_db)
    run(
        blank_db,
        "INSERT INTO accounts (account_id, customer_id, kind, name, balance_fen, covered,"
        " created_at) VALUES ('acc_1', 'cus_main', 'customer', 'a', 0, 1, 't')",
        OPERATION.format(op="opn_1", key="k"),
        "INSERT INTO reservations (reservation_id, operation_id, account_id, amount_fen, state,"
        " created_at) VALUES ('rsv_1', 'opn_1', 'acc_1', 100, 'ACTIVE', 't')",
        "UPDATE reservations SET state = 'CONSUMED'",
    )
    must_fail(blank_db, "UPDATE reservations SET state = 'RELEASED'", match="once|already")
    must_fail(
        blank_db,
        "INSERT INTO reservations (reservation_id, operation_id, account_id, amount_fen, state,"
        " created_at) VALUES ('rsv_2', 'opn_1', 'acc_1', 100, 'ACTIVE', 't')",
        match="UNIQUE",
    )


@pytest.mark.parametrize(
    "table", ["audit_log", "domain_events", "operation_events", "failure_records"]
)
def test_append_only_tables_refuse_updates(db: Database, table: str):
    with db.unit_of_work("seed") as uow:
        audit.record(uow, AuditActor.SYSTEM, "seed")
        events.append_event(
            uow, DomainEventType.CLOCK_ADVANCED, ClockAdvanced(from_at=EPOCH, to_at=EPOCH)
        )
        uow.execute(OPERATION.format(op="opn_1", key="k"))
        uow.execute(
            "INSERT INTO operation_events (operation_id, event, at)"
            " VALUES ('opn_1', 'SUBMITTED', 't')"
        )
    failures.record_failure(db, error_class=ErrorClass.UNEXPECTED, detail={"exception": "E"})
    must_fail(db, f"UPDATE {table} SET rowid = rowid", match="append-only")


def test_one_active_lock_per_scope(blank_db: Database):
    lock = (
        "INSERT INTO locks (lock_id, scope, scope_ref, reason, since, created_at)"
        " VALUES ('{id}', 'customer', 'cus_main', 'r', 't', 't')"
    )
    run(blank_db, lock.format(id="lck_1"))
    must_fail(blank_db, lock.format(id="lck_2"), match="UNIQUE")


def test_im_messages_are_seen_once(blank_db: Database):
    seen = (
        "INSERT INTO im_messages_seen (channel, message_id, created_at)"
        " VALUES ('feishu', 'm1', 't')"
    )
    run(blank_db, seen)
    must_fail(blank_db, seen, match="UNIQUE")


def test_foreign_keys_are_enforced(blank_db: Database):
    must_fail(
        blank_db,
        "INSERT INTO payees (payee_id, customer_id, alias, display_name, bank, account_masked,"
        " account_ref, created_at)"
        " VALUES ('pay_x', 'cus_ghost', 'a', 'a', 'same', '··1', 'r', 't')",
        match="FOREIGN KEY",
    )
