"""Statement import and reconciliation on the base profile (SPEC §13.3, S2 §10)."""

from datetime import date

from wg.k6 import fixture, imports
from wg.k6.cash_inputs import cash_inputs_from_db
from wg.k6.db import Database
from wg.k6.migrate import migrate
from wg.k6.simtime import read_clock
from wg.numeric import compute_snapshot

MAIN = "cus_main"


def fixture_statement(db, statement_id: str):
    profile = fixture.read_profile("fixture_v1")
    source = next(s for s in profile.statements if s.id == statement_id)
    with db.read() as conn:
        epoch_date = read_clock(conn).epoch_date
    return fixture.statement_of(source, epoch_date)


def values_of(db):
    with db.read() as conn:
        return compute_snapshot(cash_inputs_from_db(conn, MAIN))


def test_loader_statements_reproduce_the_golden_values(db):
    values = values_of(db)
    assert (values.c_fen, values.protected_fen, values.headroom_fen) == (
        1_800_000,
        1_600_000,
        200_000,
    )
    assert (values.e_fen, values.l_fen, values.b_fen, values.m_fen) == (
        400_000,
        300_000,
        840_000,
        60_000,
    )
    assert values.monthly_estimate_fen == 0
    assert not values.quality.incomplete and all(values.quality.reconciled.values())
    with db.read() as conn:
        batches = conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0]
    assert batches == len(fixture.read_profile("fixture_v1").statements)


def test_reimport_counts_duplicates_and_changes_nothing(db):
    statement = fixture_statement(db, "stm_main_2610")
    with db.read() as conn:
        lines_before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    before = values_of(db)

    outcome = db.run_in_uow(lambda uow: imports.import_statement(uow, statement), "import")

    assert (outcome.new_count, outcome.duplicate_count) == (0, len(statement.lines))
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == lines_before
        rec = imports.reconciliation(conn, statement.account_id)
    assert rec.reconciled
    assert values_of(db).inputs_hash == before.inputs_hash


def test_a_new_line_is_stored_once_and_shows_as_a_difference(db):
    statement = fixture_statement(db, "stm_main_2611")
    extra = imports.StatementLine(
        source_txn_id="MAIN-202611-099",
        posted_on=date(2026, 11, 2),
        amount_fen=-5_000,
        counterparty="模拟商户",
        description="新增",
        category_hint="shopping",
    )
    longer = imports.Statement(
        statement.statement_id,
        statement.account_id,
        statement.period,
        statement.opening_fen,
        statement.closing_fen,
        (*statement.lines, extra),
    )
    first = db.run_in_uow(lambda uow: imports.import_statement(uow, longer), "import")
    second = db.run_in_uow(lambda uow: imports.import_statement(uow, longer), "import")

    assert (first.new_count, second.new_count) == (1, 0)
    with db.read() as conn:
        rec = imports.reconciliation(conn, statement.account_id)
    # The bank's closing balance does not include the extra line: it shows as a difference.
    assert not rec.reconciled and rec.difference_fen == 5_000
    assert values_of(db).quality.incomplete


def test_a_missing_line_makes_the_account_unreconciled(tmp_path):
    database = Database(tmp_path / "t35.db")
    migrate(database)
    fixture.load_into_empty(database, fixture.read_profile("fixture_v1", "t35"), "t35")
    with database.read() as conn:
        quality = compute_snapshot(cash_inputs_from_db(conn, MAIN)).quality
        rec = imports.reconciliation(conn, "acc_main")
    assert quality.incomplete
    assert quality.reconciled == {"acc_main": False, "acc_savings": True}
    assert rec.difference_fen != 0
    database.close()


def test_unresolved_line_keeps_its_impact(db):
    quality = values_of(db).quality
    assert (quality.unresolved_count, quality.unresolved_impact_fen) == (1, 200_000)
    with db.read() as conn:
        lines = imports.customer_lines(conn, MAIN, since=date(1970, 1, 1), unresolved_only=True)
    assert [line.amount_fen for line in lines] == [-200_000]


def test_resolving_a_line_classifies_it_for_the_customer_only(db):
    with db.read() as conn:
        [line] = imports.customer_lines(conn, MAIN, since=date(1970, 1, 1), unresolved_only=True)
    db.run_in_uow(
        lambda uow: imports.resolve_line(
            uow, line.account_id, line.source_txn_id, "personal_transfer"
        ),
        "resolve",
    )
    with db.read() as conn:
        found = imports.find_line(conn, MAIN, line.source_txn_id)
        assert found is not None and found.resolved and found.category == "personal_transfer"
        assert found.resolution == "customer"
    assert values_of(db).quality.unresolved_count == 0
