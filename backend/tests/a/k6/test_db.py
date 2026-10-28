import sqlite3
import threading

import pytest

from wg.contracts.errors import ErrorCode, WgError
from wg.k6 import failures
from wg.k6.db import (
    BUSY_RETRIES,
    Database,
    NestedTransactionError,
    in_transaction,
    open_database,
)

from .helpers import add_customer, add_plan, count

pytestmark = pytest.mark.layer("D")


def insert_customer(uow, customer_id: str) -> None:
    uow.execute(
        "INSERT INTO customers (customer_id, handle, password_hash, display_name, created_at)"
        " VALUES (?, ?, 'x', 'n', 't')",
        (customer_id, customer_id),
    )


def test_write_connection_pragmas(blank_db: Database):
    with blank_db.unit_of_work("pragmas") as uow:
        assert uow.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert uow.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert uow.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert uow.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_unit_commits_on_success(blank_db: Database):
    with blank_db.unit_of_work("insert") as uow:
        insert_customer(uow, "cus_a")
    assert count(blank_db, "customers") == 1


def test_unit_rolls_back_on_exception(blank_db: Database):
    with pytest.raises(RuntimeError), blank_db.unit_of_work("insert") as uow:
        insert_customer(uow, "cus_a")
        raise RuntimeError("boom")
    assert count(blank_db, "customers") == 0
    assert not in_transaction()


def test_in_transaction_only_inside_a_unit(blank_db: Database):
    assert not in_transaction()
    with blank_db.unit_of_work("probe"):
        assert in_transaction()
    assert not in_transaction()


@pytest.mark.case("R8")
def test_nested_unit_raises_and_outer_unit_still_decides(blank_db: Database):
    with blank_db.unit_of_work("outer") as uow:
        insert_customer(uow, "cus_a")
        with pytest.raises(NestedTransactionError):
            with blank_db.unit_of_work("inner"):
                pass
        with pytest.raises(NestedTransactionError):
            blank_db.run_in_uow(lambda inner: None, "inner")
        insert_customer(uow, "cus_b")
    assert count(blank_db, "customers") == 2


@pytest.mark.case("R8")
def test_nesting_across_two_databases_also_raises(blank_db: Database, tmp_path):
    other = Database(tmp_path / "other.db")
    with blank_db.unit_of_work("outer"):
        with pytest.raises(NestedTransactionError):
            with other.unit_of_work("inner"):
                pass
    other.close()


@pytest.mark.case("R10")
def test_read_only_connection_refuses_writes(blank_db: Database):
    add_customer(blank_db, "cus_a")
    with blank_db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
        for statement in (
            "INSERT INTO customers VALUES ('cus_b', 'b', 'x', 'n', 't')",
            "UPDATE customers SET display_name = 'changed'",
            "DELETE FROM customers",
            "CREATE TABLE intruder (x)",
        ):
            with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
                conn.execute(statement)
    with blank_db.read() as conn:
        assert conn.execute("SELECT display_name FROM customers").fetchone()[0] == "测试"


@pytest.mark.case("R10")
def test_read_only_mode_holds_without_query_only(blank_db: Database):
    add_customer(blank_db, "cus_a")
    with blank_db.read() as conn:
        conn.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM customers")


def test_readers_do_not_see_an_open_unit(blank_db: Database):
    with blank_db.unit_of_work("pending") as uow:
        insert_customer(uow, "cus_a")
        with blank_db.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0
    assert count(blank_db, "customers") == 1


def test_units_from_many_threads_are_serialized(blank_db: Database):
    blank_db.run_in_uow(lambda uow: insert_customer(uow, "cus_a"), "seed")

    def bump(uow):
        name = uow.execute("SELECT display_name FROM customers").fetchone()[0]
        uow.execute("UPDATE customers SET display_name = ?", (name + "x",))

    threads = [threading.Thread(target=blank_db.run_in_uow, args=(bump, "bump")) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with blank_db.read() as conn:
        assert conn.execute("SELECT display_name FROM customers").fetchone()[0] == "n" + "x" * 8


def test_open_database_returns_one_instance_per_file(tmp_path):
    assert open_database(tmp_path / "a.db") is open_database(tmp_path / "a" / ".." / "a.db")
    assert open_database(tmp_path / "a.db") is not open_database(tmp_path / "b.db")


def busy_error() -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return exc


def test_busy_unit_is_retried_whole(blank_db: Database):
    calls = []

    def flaky(uow):
        calls.append(1)
        insert_customer(uow, f"cus_{len(calls)}")
        if len(calls) < 3:
            raise busy_error()
        return "done"

    assert blank_db.run_in_uow(flaky, "flaky") == "done"
    assert len(calls) == 3
    with blank_db.read() as conn:
        rows = conn.execute("SELECT customer_id FROM customers").fetchall()
    assert [row[0] for row in rows] == ["cus_3"]
    assert count(blank_db, "failure_records") == 0


def test_busy_from_another_connection_ends_in_busy_retry(blank_db: Database):
    db = Database(blank_db.path, busy_timeout_ms=10)
    holder = sqlite3.connect(blank_db.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    calls = []
    try:
        with pytest.raises(WgError) as raised:
            db.run_in_uow(lambda uow: calls.append(1), "blocked")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert raised.value.code is ErrorCode.BUSY_RETRY
    assert raised.value.detail["attempts"] == BUSY_RETRIES + 1
    assert calls == []
    assert db.run_in_uow(lambda uow: "free", "after") == "free"
    assert count(blank_db, "failure_records") == 0
    db.close()


@pytest.mark.case("R8")
def test_unexpected_failure_rolls_back_effect_but_keeps_evidence(db: Database):
    add_plan(db, "pln_one")

    def failing(uow):
        insert_customer(uow, "cus_new")
        uow.execute("UPDATE plans SET status = 'completed' WHERE plan_id = 'pln_one'")
        raise ZeroDivisionError("division by zero")

    for expected in (1, 2):
        with pytest.raises(ZeroDivisionError):
            db.run_in_uow(failing, "submit", plan_id="pln_one", logical_action_id="act_x")
        with db.read() as conn:
            assert failures.breaker_count(conn, "pln_one") == expected

    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM plans").fetchone()[0] == "active"
        records = failures.failure_records(conn, "pln_one")
        state = failures.breaker_state(conn, "pln_one")
    assert len(records) == 2
    assert records[0]["logical_action_id"] == "act_x"
    assert records[0]["error_class"] == "unexpected"
    assert records[0]["detail"]["exception"] == "ZeroDivisionError"
    assert state.consecutive_unexpected == 2 and state.last_failure_at is not None


def test_success_resets_the_breaker(db: Database):
    add_plan(db, "pln_one")
    with pytest.raises(KeyError):
        db.run_in_uow(lambda uow: {}["x"], "submit", plan_id="pln_one")
    db.run_in_uow(lambda uow: failures.record_success(uow, "pln_one"), "settled")
    with db.read() as conn:
        state = failures.breaker_state(conn, "pln_one")
    assert state.consecutive_unexpected == 0
    assert state.last_success_at is not None


@pytest.mark.parametrize(
    "code", [ErrorCode.INSUFFICIENT_FUNDS, ErrorCode.PROVIDER_TIMEOUT, ErrorCode.OUTCOME_UNKNOWN]
)
def test_expected_transient_and_unknown_do_not_count(db: Database, code: ErrorCode):
    add_plan(db, "pln_one")

    def refuse(uow):
        raise WgError(code)

    with pytest.raises(WgError):
        db.run_in_uow(refuse, "x", plan_id="pln_one")
    assert count(db, "failure_records") == 0
    assert count(db, "breaker_state") == 0


def test_integrity_failure_is_recorded_without_counting(db: Database):
    add_plan(db, "pln_one")

    def broken(uow):
        raise WgError(ErrorCode.INTEGRITY_VIOLATION, {"check": "ledger"})

    with pytest.raises(WgError):
        db.run_in_uow(broken, "settle", plan_id="pln_one")
    with db.read() as conn:
        records = failures.failure_records(conn)
    assert [(r["error_class"], r["detail"]["code"]) for r in records] == [
        ("integrity", "INTEGRITY_VIOLATION")
    ]
    assert count(db, "breaker_state") == 0


def test_failure_for_a_plan_created_in_the_failed_unit_is_still_recorded(db: Database):
    add_customer(db, "cus_main")

    def create_then_fail(uow):
        uow.execute(
            "INSERT INTO plans (plan_id, customer_id, kind, status, mode, created_at)"
            " VALUES ('pln_new', 'cus_main', 'single', 'draft', 'coordinated', 't')"
        )
        raise RuntimeError("after insert")

    with pytest.raises(RuntimeError):
        db.run_in_uow(create_then_fail, "compose", plan_id="pln_new")
    assert count(db, "plans") == 0
    assert count(db, "failure_records") == 1
    assert count(db, "breaker_state") == 0
