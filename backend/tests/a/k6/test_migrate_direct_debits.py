"""Schema 7: the direct-debit tables and the `actions` rebuild in the foreign-keys-off mode.

The populated database is built through the HTTP app on schema 6, as a release build would
have left it, then migrated (S5-SPEC §4.4).
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from wg.api import app as app_module
from wg.contracts.api import (
    ApprovalRequestResult,
    CandidatesView,
    Card,
    ConfirmResult,
    DraftResult,
    PlanDraftResult,
    SmsView,
    VerifyResult,
)
from wg.contracts.enums import ActionKind, ActionStatus
from wg.k6.db import Database, NestedTransactionError, in_transaction, open_database
from wg.k6.migrate import MigrationError, current_version, discover, migrate

from ..api.conftest import Browser, make_client
from .helpers import add_customer

pytestmark = pytest.mark.layer("D")

S5_TABLES = {
    "merchants",
    "mandates",
    "mandate_events",
    "debit_requests",
    "effect_verification_events",
}
ACTION_CHILDREN = {"challenges", "operations", "cards"}
SEARCH = {"sector": "technology", "target_max_pct": "40.0000", "fee_cap_fen": 2_000}


class Rollback(Exception):
    pass


def schema_dir(tmp_path: Path, last: int, extra_sql: str = "") -> Path:
    """Migrations 1..last copied into a directory; `extra_sql` is appended to the last one."""
    directory = tmp_path / f"schema_{last}{'_changed' if extra_sql else ''}"
    directory.mkdir()
    for migration in discover()[:last]:
        shutil.copy(migration.path, directory / migration.path.name)
    if extra_sql:
        path = directory / discover()[last - 1].path.name
        path.write_text(path.read_text(encoding="utf-8") + extra_sql, encoding="utf-8")
    return directory


def query(db: Database, sql: str, *params) -> list[tuple]:
    with db.read() as conn:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]


def user_tables(db: Database) -> list[str]:
    return [
        row[0]
        for row in query(
            db,
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name",
        )
    ]


def contents(db: Database, tables: list[str]) -> dict[str, list[str]]:
    """Every row of every table, as sorted text, so a rebuild's row order does not matter."""
    return {t: sorted(repr(row) for row in query(db, f'SELECT * FROM "{t}"')) for t in tables}


def foreign_keys(db: Database, tables: list[str]) -> dict[str, list[tuple]]:
    return {t: sorted(query(db, f"PRAGMA foreign_key_list('{t}')")) for t in tables}


def action_indexes(db: Database) -> dict[str, str]:
    rows = query(
        db,
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'actions'"
        " AND sql IS NOT NULL",
    )
    return {name: " ".join(sql.split()) for name, sql in rows}


def write_pragma(db: Database, pragma: str) -> list[tuple]:
    """A pragma read on the write connection, where foreign key enforcement is set."""
    with db.unit_of_work("pragma") as uow:
        return [tuple(row) for row in uow.execute(f"PRAGMA {pragma}").fetchall()]


def ok(response, model):
    assert response.status_code in (200, 201), response.text
    return model.model_validate(response.json())


def card_of(browser: Browser, logical_action_id: str) -> Card:
    result = ok(
        browser.post(f"/v1/actions/{logical_action_id}/request-approval"), ApprovalRequestResult
    )
    return ok(browser.get(f"/v1/cards/{result.card_id}"), Card)


def draft(browser: Browser, amount_fen: int) -> DraftResult:
    body = {"payee_id": "pay_mom", "amount_fen": amount_fen}
    return ok(browser.post("/v1/drafts/transfer", body), DraftResult)


def confirm(browser: Browser, amount_fen: int) -> ConfirmResult:
    card = card_of(browser, draft(browser, amount_fen).logical_action_id)
    body = {"terms_hash": card.terms_hash}
    return ok(browser.post(f"/v1/cards/{card.card_id}/confirm", body), ConfirmResult)


def sms_code(browser: Browser, challenge_id: str) -> str:
    messages = ok(browser.get("/v1/sms"), SmsView).messages
    return next(m.code for m in messages if m.challenge_id == challenge_id)


def verify(browser: Browser, challenge_id: str, code: str) -> VerifyResult:
    body = {"code": code}
    return ok(browser.post(f"/v1/challenges/{challenge_id}/verify", body), VerifyResult)


def logged_in(client) -> Browser:
    browser = Browser(client)
    browser.login("opr_demo")
    browser.login("cus_main")
    return browser


def populate_schema_6(db_path: Path, schema_6: Path) -> dict[str, str]:
    """A settled same-bank transfer, a RED transfer with an open challenge, a draft, a plan."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app_module, "migrate", lambda db: migrate(db, schema_6))
        with make_client(db_path) as client:
            browser = logged_in(client)
            settled = confirm(browser, 50_000)
            assert settled.action_status is ActionStatus.SETTLED
            red = confirm(browser, 100_000)
            assert red.action_status is ActionStatus.AWAITING_STEPUP and red.challenge
            challenge_id = red.challenge.challenge_id
            plain = draft(browser, 10_000)
            found = ok(browser.post("/v1/funds/candidates", SEARCH), CandidatesView)
            body = {"calc_result_id": found.calc_result_id}
            plan = ok(browser.post("/v1/funds/plans", body), PlanDraftResult)
            assert plan.plan_id is not None
            return {
                "red": red.logical_action_id or "",
                "challenge_id": challenge_id,
                "code": sms_code(browser, challenge_id),
                "plain": plain.logical_action_id,
                "plan": plan.plan_id,
            }


@pytest.fixture
def populated(tmp_path: Path) -> Iterator[tuple[Database, dict[str, str]]]:
    db_path = tmp_path / "wg.db"
    flows = populate_schema_6(db_path, schema_dir(tmp_path, 6))
    db = open_database(db_path)
    with db.read() as conn:
        assert current_version(conn) == 6
    yield db, flows
    db.close()


def test_only_schema_7_runs_without_foreign_keys():
    assert [m.version for m in discover() if m.foreign_keys_off] == [7]


def test_migrating_a_populated_database_keeps_every_row_and_reference(populated):
    db, flows = populated
    old_tables = [t for t in user_tables(db) if t != "schema_version"]
    before = contents(db, old_tables)
    keys_before = foreign_keys(db, old_tables)
    indexes_before = action_indexes(db)
    assert query(db, "SELECT COUNT(*) FROM operations WHERE kind = 'TRANSFER'") == [(1,)]
    assert query(db, "SELECT COUNT(*) FROM plans WHERE kind = 'rebalance'") == [(1,)]

    assert migrate(db) == 7

    assert set(user_tables(db)) == set(old_tables) | S5_TABLES | {"schema_version"}
    assert query(db, "SELECT version, name FROM schema_version WHERE version > 6") == [
        (7, "direct_debits")
    ]
    assert contents(db, old_tables) == before
    assert foreign_keys(db, old_tables) == keys_before
    for child in ACTION_CHILDREN:
        assert "actions" in {row[2] for row in keys_before[child]}
    assert action_indexes(db) == indexes_before
    assert set(indexes_before) == {
        "actions_one_active_revision",
        "actions_by_plan",
        "actions_by_customer",
    }
    assert query(db, "PRAGMA foreign_key_check") == []
    assert query(db, "PRAGMA integrity_check") == [("ok",)]
    assert write_pragma(db, "foreign_keys") == [(1,)]
    assert flows["plain"] in {row[0] for row in query(db, "SELECT logical_action_id FROM actions")}


def test_the_rebuilt_actions_table_takes_the_new_kinds_only(populated):
    db, _ = populated
    migrate(db)
    insert = (
        "INSERT INTO actions (logical_action_id, revision, customer_id, kind, status,"
        " params_json, request_hash, tier, created_at)"
        " VALUES ('act_probe', 1, 'cus_main', ?, 'DRAFT', '{}', 'h', 'YELLOW', 't')"
    )
    for kind in ActionKind:
        with pytest.raises(Rollback):
            with db.unit_of_work("kind check") as uow:
                uow.execute(insert, (kind.value,))
                raise Rollback
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        with db.unit_of_work("unknown kind") as uow:
            uow.execute(insert, ("NOT_A_KIND",))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with db.unit_of_work("orphan card") as uow:
            uow.execute(
                "INSERT INTO cards (card_id, customer_id, kind, logical_action_id, revision,"
                " tier, terms_json, terms_hash, content_json, expires_at, created_at)"
                " VALUES ('crd_x', 'cus_main', 'action', 'act_ghost', 1, 'YELLOW', '{}', 'h',"
                " '{}', 't', 't')"
            )


def test_after_the_migration_the_product_verifies_old_codes_transfers_and_resets(populated):
    db, flows = populated
    with make_client(db.path) as client:
        browser = logged_in(client)
        assert query(db, "SELECT MAX(version) FROM schema_version") == [(7,)]
        verified = verify(browser, flows["challenge_id"], flows["code"])
        assert verified.logical_action_id == flows["red"]
        assert verified.action_status is ActionStatus.SETTLED
        fresh = confirm(browser, 20_000)
        if fresh.action_status is ActionStatus.AWAITING_STEPUP:
            assert fresh.challenge is not None
            challenge_id = fresh.challenge.challenge_id
            code = sms_code(browser, challenge_id)
            assert verify(browser, challenge_id, code).action_status is ActionStatus.SETTLED
        else:
            assert fresh.action_status is ActionStatus.SETTLED
        assert query(db, "SELECT COUNT(*) FROM receipts WHERE status = 'SETTLED'") == [(3,)]
        assert query(db, "PRAGMA foreign_key_check") == []
        assert browser.sim("/sim/reset", {}).status_code == 200
        assert query(db, "SELECT COUNT(*) FROM actions") == [(0,)]
        assert query(db, "PRAGMA foreign_key_check") == []


@pytest.mark.parametrize(
    ("extra_sql", "error"),
    [
        # The check after the statements finds an agreement event without its agreement.
        (
            "\nINSERT INTO mandate_events (mandate_ref, field, new, actor, at)"
            " VALUES ('mdt_ghost', 'status', 'ACTIVE', 'system', 't');\n",
            MigrationError,
        ),
        ("\nSELECT no_such_function();\n", sqlite3.OperationalError),
    ],
    ids=["foreign key check", "statement error"],
)
def test_a_failure_inside_the_rebuild_leaves_version_6_with_foreign_keys_on(
    tmp_path: Path, extra_sql: str, error: type[Exception]
):
    db = Database(tmp_path / "wg.db")
    migrate(db, schema_dir(tmp_path, 6))
    add_customer(db)
    with db.unit_of_work("action") as uow:
        uow.execute(
            "INSERT INTO actions (logical_action_id, revision, customer_id, kind, status,"
            " params_json, request_hash, tier, created_at)"
            " VALUES ('act_1', 1, 'cus_main', 'TRANSFER', 'DRAFT', '{}', 'h', 'YELLOW', 't')"
        )
    faulty = schema_dir(tmp_path, 7, extra_sql)

    with pytest.raises(error):
        migrate(db, faulty)

    with db.read() as conn:
        assert current_version(conn) == 6
    assert not S5_TABLES & set(user_tables(db))
    assert query(db, "SELECT kind FROM actions") == [("TRANSFER",)]
    assert write_pragma(db, "foreign_keys") == [(1,)]
    assert not in_transaction()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        with db.unit_of_work("new kind on schema 6") as uow:
            uow.execute("UPDATE actions SET kind = 'DEBIT_PAY'")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with db.unit_of_work("orphan") as uow:
            uow.execute(
                "INSERT INTO accounts (account_id, customer_id, kind, name, balance_fen,"
                " covered, created_at) VALUES ('acc_x', 'cus_ghost', 'customer', 'a', 0, 1, 't')"
            )
    assert migrate(db) == 7
    db.close()


def test_the_foreign_keys_off_unit_is_a_unit_of_work(blank_db: Database):
    with blank_db.unit_of_work_without_foreign_keys("rebuild") as uow:
        assert in_transaction()
        assert [tuple(r) for r in uow.execute("PRAGMA foreign_keys")] == [(0,)]
        with pytest.raises(NestedTransactionError):
            with blank_db.unit_of_work("nested"):
                pass
    assert not in_transaction()
    assert write_pragma(blank_db, "foreign_keys") == [(1,)]
    with blank_db.unit_of_work("outer"):
        with pytest.raises(NestedTransactionError):
            with blank_db.unit_of_work_without_foreign_keys("nested"):
                pass
