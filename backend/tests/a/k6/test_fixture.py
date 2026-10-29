import json
from datetime import timedelta
from decimal import Decimal

import pytest

from wg.contracts.enums import Fault
from wg.contracts.fixture import FixtureError, FixtureLot
from wg.contracts.tools import READ_SCOPES
from wg.k6 import faults, fixture
from wg.k6.clock import advance_clock
from wg.k6.db import Database
from wg.k6.migrate import discover, migrate
from wg.k6.passwords import verify_password
from wg.k6.simtime import read_clock

from .helpers import EPOCH, count

pytestmark = pytest.mark.layer("D")

BASE = fixture.read_profile("fixture_v1")
VARIANTS = sorted(BASE.variants)


def rows(db: Database, sql: str, params: tuple = ()) -> list[tuple]:
    with db.read() as conn:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]


@pytest.fixture
def loaded(blank_db: Database) -> Database:
    fixture.load_into_empty(blank_db, BASE)
    return blank_db


def test_every_documented_variant_is_present():
    assert {"t25", "t35", "t42", "t43", "t52", "t64", "t65"} <= set(VARIANTS)


@pytest.mark.parametrize("variant", [None, *VARIANTS])
def test_profile_and_every_variant_load(blank_db: Database, variant: str | None):
    profile = fixture.read_profile("fixture_v1", variant)
    clock = fixture.load_into_empty(blank_db, profile, variant)
    assert clock.now == EPOCH and clock.day == 0
    assert rows(blank_db, "SELECT profile, variant FROM sim_profile") == [("fixture_v1", variant)]
    lines = sum(len(s.transactions) for s in profile.statements)
    assert count(blank_db, "transactions") == lines
    assert count(blank_db, "lots") == len(profile.lots)
    for account in profile.accounts:
        assert rows(
            blank_db, "SELECT balance_fen FROM accounts WHERE account_id = ?", (account.id,)
        ) == [(account.opening_fen,)]
    assert_ledger_balanced(blank_db)


def assert_ledger_balanced(db: Database) -> None:
    assert (
        rows(
            db,
            "SELECT operation_id FROM ledger_entries GROUP BY operation_id HAVING SUM(amount_fen)",
        )
        == []
    )
    assert (
        rows(
            db,
            "SELECT a.account_id FROM accounts a LEFT JOIN ledger_entries e USING (account_id)"
            " GROUP BY a.account_id HAVING a.balance_fen <> COALESCE(SUM(e.amount_fen), 0)",
        )
        == []
    )


def test_base_profile_content(loaded: Database):
    assert count(loaded, "customers") == 2 and count(loaded, "operators") == 2
    # One own-account payee per customer account follows the fixture's payees.
    assert count(loaded, "payees") == len(BASE.payees) + len(BASE.accounts)
    assert rows(loaded, "SELECT kind, status, idempotency_key FROM operations") == [
        ("OPENING", "SETTLED", "opening:fixture_v1")
    ]
    assert rows(
        loaded,
        "SELECT account_id, balance_fen FROM accounts WHERE kind = 'internal' AND balance_fen <> 0",
    ) == [("acc_sys_opening_equity", -2300000)]
    assert rows(
        loaded, "SELECT commitment_id, due_date, unpaid_fen, status FROM commitments ORDER BY 1"
    ) == [
        ("cmt_insurance", "2026-11-09", 500000, "CONFIRMED"),
        ("cmt_rent", "2026-11-04", 340000, "CONFIRMED"),
    ]
    assert rows(loaded, "SELECT customer_id, kind, target_fen FROM reserves") == [
        ("cus_main", "emergency", 400000)
    ]
    assert rows(loaded, "SELECT period, budget_fen, excluded_purpose_ids FROM allowances") == [
        ("2026-11", 360000, '["pur_rent"]')
    ]
    [(margin, constants)] = rows(
        loaded, "SELECT margin_fen, plan_constants_json FROM customer_settings"
    )
    assert margin == 60000
    assert json.loads(constants)["bounds"]["p0_fen"] == 498500
    assert rows(
        loaded,
        "SELECT posted_on, amount_fen, counterparty_raw, category_hint FROM transactions"
        " WHERE source_txn_id = 'MAIN-202610-007'",
    ) == [("2026-10-15", -200000, "李明", None)]
    assert rows(loaded, "SELECT nav, nav_date FROM products WHERE product_id = '990003'") == [
        ("1.0000", "2026-11-02")
    ]
    assert rows(
        loaded, "SELECT account_id, shares, acquired_on FROM lots WHERE lot_id = 'lot_c1'"
    ) == [("acc_main", "10000.0000", "2026-09-03")]
    tech = rows(
        loaded,
        "SELECT weight FROM holdings_snapshots WHERE product_id = '990003'"
        " AND sector_code = 'technology'",
    )
    assert sum(Decimal(w) for (w,) in tech) == Decimal("0.7000")


def test_logins_are_salted_hashes(loaded: Database):
    stored = dict(rows(loaded, "SELECT handle, password_hash FROM customers"))
    stored |= dict(rows(loaded, "SELECT handle, password_hash FROM operators"))
    for login in [*BASE.customers, *BASE.operators]:
        assert login.password not in stored[login.handle]
        assert verify_password(login.password, stored[login.handle])
    assert len(set(stored.values())) == len(stored)


def test_read_scopes_are_seeded_from_the_registry(loaded: Database):
    seeded = {
        intent: set(json.loads(sources))
        for intent, sources in rows(loaded, "SELECT intent, allowed_sources FROM read_scopes")
    }
    assert seeded == {i.value: {s.value for s in srcs} for i, srcs in READ_SCOPES.items()}


def test_variants_change_what_they_name(blank_db: Database, tmp_path):
    fixture.load_into_empty(blank_db, fixture.read_profile("fixture_v1", "t25"), "t25")
    assert rows(
        blank_db,
        "SELECT account_id, balance_fen FROM accounts WHERE kind = 'customer'"
        " AND customer_id = 'cus_main' ORDER BY 1",
    ) == [
        ("acc_main", 200000),
        ("acc_savings", 1600000),
    ]
    other = Database(tmp_path / "t43.db")
    migrate(other)
    fixture.load_into_empty(other, fixture.read_profile("fixture_v1", "t43"), "t43")
    assert rows(other, "SELECT lot_id FROM lots WHERE product_id = '990003'") == []
    other.close()


def test_load_refuses_a_database_that_holds_a_profile(loaded: Database):
    with pytest.raises(fixture.NotEmptyError):
        fixture.load_into_empty(loaded, BASE)
    assert count(loaded, "customers") == 2


@pytest.mark.case("R8")
def test_reset_replaces_everything_and_rewinds_the_clock(loaded: Database):
    with loaded.unit_of_work("play") as uow:
        advance_clock(uow, EPOCH + timedelta(days=3))
        faults.arm(uow, Fault.BANK_ACK_LOST, persistent=True)
    clock = fixture.reset(loaded, fixture.read_profile("fixture_v1", "t25"), "t25")
    assert clock.now == EPOCH
    assert count(loaded, "faults") == 0
    assert count(loaded, "domain_events") == 0
    assert rows(loaded, "SELECT event FROM audit_log") == [("fixture_loaded",)]
    assert rows(loaded, "SELECT variant FROM sim_profile") == [("t25",)]
    assert count(loaded, "operations") == 1
    assert_ledger_balanced(loaded)


def test_failed_reset_keeps_the_previous_state(loaded: Database):
    with loaded.unit_of_work("play") as uow:
        advance_clock(uow, EPOCH + timedelta(days=1))
    orphan = FixtureLot.model_validate(
        {
            "id": "lot_x",
            "product": "990009",
            "shares": "1.0000",
            "acquired_offset_days": -1,
            "cost_fen": 100,
        }
    )
    broken = BASE.model_copy(update={"lots": (*BASE.lots, orphan)})
    with pytest.raises(FixtureError, match="exactly one position"):
        fixture.reset(loaded, broken)
    with loaded.read() as conn:
        assert read_clock(conn).day == 1
    assert count(loaded, "customers") == 2


def test_profile_names_are_checked():
    for name in ("../fixture_v1", "Fixture", "", "fixture_v1.yaml"):
        with pytest.raises(FixtureError):
            fixture.read_profile(name)
    with pytest.raises(FixtureError, match="unknown profile"):
        fixture.read_profile("fixture_missing")
    with pytest.raises(FixtureError, match="unknown variant"):
        fixture.read_profile("fixture_v1", "t99")


def test_command_line_load(tmp_path, capsys):
    path = tmp_path / "cli.db"
    assert (
        fixture.main(["load", "--profile", "fixture_v1", "--variant", "t52", "--db", str(path)])
        == 0
    )
    out = capsys.readouterr().out
    assert "variant=t52" in out and f"schema={len(discover())}" in out
    assert "clock=2026-11-02T09:00:00+08:00" in out
    assert fixture.main(["load", "--db", str(path)]) == 1
    assert "already holds a profile" in capsys.readouterr().err
    assert fixture.main(["load", "--variant", "nope", "--db", str(tmp_path / "x.db")]) == 1
