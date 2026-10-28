"""Shared helpers for the K6 tests: direct inserts of the few rows the tests need."""

from datetime import datetime

from wg.k6.db import Database
from wg.k6.simtime import SHANGHAI

EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)


def add_customer(db: Database, customer_id: str = "cus_main") -> None:
    with db.unit_of_work("test customer") as uow:
        uow.execute(
            "INSERT INTO customers (customer_id, handle, password_hash, display_name, created_at)"
            " VALUES (?, ?, 'x', '测试', 't')",
            (customer_id, customer_id),
        )


def add_plan(db: Database, plan_id: str = "pln_test", customer_id: str = "cus_main") -> None:
    add_customer(db, customer_id)
    with db.unit_of_work("test plan") as uow:
        uow.execute(
            "INSERT INTO plans (plan_id, customer_id, kind, status, mode, created_at)"
            " VALUES (?, ?, 'mixed', 'active', 'coordinated', 't')",
            (plan_id, customer_id),
        )


def count(db: Database, table: str) -> int:
    with db.read() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
