"""Commitments and cash snapshots on the base profile, through the K6 repositories."""

from datetime import date

import pytest

from wg.contracts.enums import CommitmentStatus
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.events import CommitmentChange
from wg.k6 import commitments, events, fixture, reserves, snapshots
from wg.k6.db import Database
from wg.k6.migrate import migrate

MAIN = "cus_main"
START_C = 1_800_000


def test_snapshot_is_rebuilt_only_when_inputs_change(db):
    first = db.run_in_uow(lambda uow: snapshots.ensure_snapshot(uow, MAIN), "snapshot")
    same = db.run_in_uow(lambda uow: snapshots.ensure_snapshot(uow, MAIN), "snapshot")
    assert same.snapshot_id == first.snapshot_id
    with db.read() as conn:
        assert snapshots.is_current(conn, MAIN)

    db.run_in_uow(lambda uow: reserves.set_reserve_target(uow, MAIN, 500_000), "reserve")
    with db.read() as conn:
        assert not snapshots.is_current(conn, MAIN)
    changed = db.run_in_uow(lambda uow: snapshots.ensure_snapshot(uow, MAIN), "snapshot")
    assert changed.snapshot_id != first.snapshot_id
    assert (changed.values.protected_fen, changed.values.headroom_fen) == (1_700_000, 100_000)
    # The earlier snapshot is immutable and still addressable.
    with db.read() as conn:
        old = snapshots.find_snapshot(conn, first.snapshot_id)
    assert old is not None and old.values.protected_fen == 1_600_000


def test_snapshot_rows_cannot_be_updated(db):
    snapshot = db.run_in_uow(lambda uow: snapshots.ensure_snapshot(uow, MAIN), "snapshot")
    with pytest.raises(Exception, match="immutable"):
        db.run_in_uow(
            lambda uow: uow.execute(
                "UPDATE cash_snapshots SET c_fen = 0 WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            ),
            "tamper",
        )


def test_transition_writes_event_row_and_domain_event_together(db):
    with db.read() as conn:
        version_before = commitments.commitments_version(conn, MAIN)
        bill = commitments.get_commitment(conn, "cmt_rent")

    paid = db.run_in_uow(
        lambda uow: commitments.transition(
            uow,
            bill,
            to=CommitmentStatus.PARTIALLY_PAID,
            change=CommitmentChange.PARTIALLY_PAID,
            delta_fen=-100_000,
            unpaid_fen=240_000,
            source_ref="opn_test",
        ),
        "pay",
    )
    assert paid.status is CommitmentStatus.PARTIALLY_PAID and paid.version == bill.version + 1
    with db.read() as conn:
        assert commitments.commitments_version(conn, MAIN) == version_before + 1
        rows = conn.execute(
            "SELECT * FROM commitment_events WHERE commitment_id = 'cmt_rent'"
        ).fetchall()
        changed = [
            e for e in events.unprocessed_events(conn, "t") if e.type.value == "commitments_changed"
        ]
    assert [(r["change"], r["delta_fen"], r["unpaid_fen"]) for r in rows] == [
        ("partially_paid", -100_000, 240_000)
    ]
    assert len(changed) == 1
    assert changed[0].payload.commitments_version == version_before + 1
    assert changed[0].payload.commitment_event_id == rows[0]["commitment_event_id"]


def test_a_stale_row_or_illegal_transition_is_refused(db):
    with db.read() as conn:
        bill = commitments.get_commitment(conn, "cmt_rent")
    with pytest.raises(commitments.StaleCommitment):
        db.run_in_uow(
            lambda uow: commitments.transition(
                uow,
                bill,
                to=CommitmentStatus.CANDIDATE,
                change=CommitmentChange.CONFIRMED,
                delta_fen=0,
            ),
            "illegal",
        )
    db.run_in_uow(
        lambda uow: commitments.transition(
            uow,
            bill,
            to=CommitmentStatus.CANCELLED,
            change=CommitmentChange.CANCELLED,
            delta_fen=-1,
        ),
        "cancel",
    )
    with pytest.raises(commitments.StaleCommitment):
        # The same read of the row again: the version has moved on.
        db.run_in_uow(
            lambda uow: commitments.transition(
                uow,
                bill,
                to=CommitmentStatus.CANCELLED,
                change=CommitmentChange.CANCELLED,
                delta_fen=-1,
            ),
            "stale",
        )


def test_commitment_events_are_append_only(db):
    with db.read() as conn:
        bill = commitments.get_commitment(conn, "cmt_rent")
    db.run_in_uow(
        lambda uow: commitments.transition(
            uow,
            bill,
            to=CommitmentStatus.CANCELLED,
            change=CommitmentChange.CANCELLED,
            delta_fen=-1,
        ),
        "cancel",
    )
    with pytest.raises(Exception, match="append-only"):
        db.run_in_uow(lambda uow: uow.execute("UPDATE commitment_events SET delta_fen = 0"), "x")


def test_unpaid_invariant_only_applies_to_owed_bills(db):
    with db.read() as conn:
        commitments.check_unpaid_invariant(conn, MAIN)
    db.run_in_uow(
        lambda uow: uow.execute(
            "UPDATE commitments SET unpaid_fen = 300000 WHERE commitment_id = 'cmt_rent'"
        ),
        "corrupt",
    )
    with db.read() as conn, pytest.raises(WgError) as broken:
        commitments.check_unpaid_invariant(conn, MAIN)
    assert broken.value.code is ErrorCode.INTEGRITY_VIOLATION
    assert broken.value.detail["customer_id"] == MAIN

    # A reported-paid bill keeps its unpaid amount without breaking the rule.
    db.run_in_uow(
        lambda uow: uow.execute(
            "UPDATE commitments SET unpaid_fen = 340000, status = 'PAID_REPORTED'"
            " WHERE commitment_id = 'cmt_rent'"
        ),
        "reset",
    )
    with db.read() as conn:
        commitments.check_unpaid_invariant(conn, MAIN)


def test_candidates_protect_nothing_and_do_not_count_as_a_change(db):
    with db.read() as conn:
        version = commitments.commitments_version(conn, MAIN)
    candidate = db.run_in_uow(
        lambda uow: commitments.insert_candidate(
            uow,
            customer_id=MAIN,
            payee_id="pay_wangfang",
            kind="培训费",
            amount_fen=120_000,
            due_date=date(2026, 11, 5),
            source="trusted_ebill",
        ),
        "candidate",
    )
    assert candidate.status is CommitmentStatus.CANDIDATE
    with db.read() as conn:
        assert commitments.commitments_version(conn, MAIN) == version
        found = commitments.find_duplicate_candidate(
            conn, MAIN, "pay_wangfang", "培训费", 120_000, date(2026, 11, 5)
        )
    assert found is not None and found.commitment_id == candidate.commitment_id
    after = db.run_in_uow(lambda uow: snapshots.ensure_snapshot(uow, MAIN), "snapshot")
    assert (after.values.protected_fen, after.values.b_fen) == (1_600_000, 840_000)


def test_snapshot_reproduces_the_golden_values_and_keeps_the_unresolved_item(db):
    snapshot = db.run_in_uow(lambda uow: snapshots.ensure_snapshot(uow, MAIN), "snapshot")
    values = snapshot.values
    assert (values.c_fen, values.protected_fen, values.headroom_fen) == (
        START_C,
        1_600_000,
        200_000,
    )
    assert [u.amount_fen for u in snapshot.unresolved] == [-200_000]
    assert not snapshot.incomplete


def test_an_incomplete_snapshot_stops_a_plan(tmp_path):
    database = Database(tmp_path / "t35.db")
    migrate(database)
    fixture.load_into_empty(database, fixture.read_profile("fixture_v1", "t35"), "t35")
    snapshot = database.run_in_uow(lambda uow: snapshots.ensure_snapshot(uow, MAIN), "snapshot")
    assert snapshot.incomplete
    with pytest.raises(WgError) as refused:
        snapshots.require_complete(snapshot)
    assert refused.value.code is ErrorCode.DATA_INCOMPLETE
    database.close()
