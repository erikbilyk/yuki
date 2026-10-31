"""Merchants and agreements loaded from a fixture profile (S5-SPEC §3.6, §10).

The profile is written to a temporary file, so these tests do not need fixture_s5.yaml.
"""

from pathlib import Path

import pytest

from wg.contracts.fixture import FixtureError, load_fixture
from wg.k6 import fixture, mandates
from wg.k6.db import Database

from ..s5_profile import s5_doc, write_profile
from .helpers import EPOCH, count

pytestmark = pytest.mark.layer("D")

S5_TABLES = ("merchants", "mandates", "mandate_events", "debit_requests")


def rows(db: Database, sql: str) -> list[tuple]:
    with db.read() as conn:
        return [tuple(row) for row in conn.execute(sql).fetchall()]


def load(db: Database, tmp_path: Path, variant: str | None = None, doc: dict | None = None):
    profile = load_fixture(write_profile(tmp_path, doc), variant)
    return fixture.load_into_empty(db, profile, variant)


def test_merchants_and_agreements_are_loaded_with_one_status_event_each(
    blank_db: Database, tmp_path: Path
):
    load(blank_db, tmp_path)
    assert rows(blank_db, "SELECT merchant_id, verified FROM merchants ORDER BY merchant_id") == [
        ("mer_cloud", 1),
        ("mer_fitness", 1),
        ("mer_music", 1),
        ("mer_unverified", 0),
        ("mer_video", 1),
    ]
    assert rows(
        blank_db,
        "SELECT mandate_ref, customer_id, account_id, merchant_id, purpose_id, expected_fen,"
        " cap_fen, debit_day, starts_on, expires_on, status, kind, period, max_per_period,"
        " cancelled_at, version FROM mandates ORDER BY mandate_ref",
    ) == [
        ("mdt_cloud", "cus_main", "acc_main", "mer_cloud", "pur_sub_cloud", 2100, 2100, 12)
        + ("2026-10-02", "2027-10-01", "ACTIVE", "pull", "monthly", 1, None, 1),
        ("mdt_fitness", "cus_main", "acc_main", "mer_fitness", "pur_sub_fitness", 19900, 22000)
        + (20, "2026-10-02", "2027-10-01", "ACTIVE", "pull", "monthly", 1, None, 1),
        ("mdt_other_video", "cus_other", "acc_other", "mer_video", "pur_sub_other_video", 2500)
        + (3000, 8, "2026-10-02", "2027-10-01", "ACTIVE", "pull", "monthly", 1, None, 1),
        ("mdt_video", "cus_main", "acc_main", "mer_video", "pur_sub_video", 2500, 3000, 5)
        + ("2026-10-02", "2027-10-01", "ACTIVE", "pull", "monthly", 1, None, 1),
    ]
    epoch = "2026-11-02T09:00:00.000000+08:00"
    assert rows(
        blank_db,
        "SELECT mandate_ref, field, old, new, operation_id, actor, reason, at"
        " FROM mandate_events ORDER BY mandate_ref",
    ) == [
        (ref, "status", None, "ACTIVE", None, "system", "fixture", epoch)
        for ref in ("mdt_cloud", "mdt_fitness", "mdt_other_video", "mdt_video")
    ]
    assert count(blank_db, "debit_requests") == 0
    with blank_db.read() as conn:
        for ref in ("mdt_cloud", "mdt_fitness", "mdt_other_video", "mdt_video"):
            mandates.check_mandate_invariant(conn, ref)
        video = mandates.find_merchant(conn, "mer_video")
    assert video is not None and video.aliases == ("视频", "视频会员", "影视会员")


def test_variants_set_the_status_and_the_term(blank_db: Database, tmp_path: Path):
    load(blank_db, tmp_path, "video_suspended")
    assert rows(blank_db, "SELECT status FROM mandates WHERE mandate_ref = 'mdt_video'") == [
        ("SUSPENDED",)
    ]
    assert rows(blank_db, "SELECT new FROM mandate_events WHERE mandate_ref = 'mdt_video'") == [
        ("SUSPENDED",)
    ]
    fixture.reset(blank_db, load_fixture(write_profile(tmp_path), "video_expiring"))
    assert rows(blank_db, "SELECT expires_on FROM mandates WHERE mandate_ref = 'mdt_video'") == [
        ("2026-11-05",)
    ]


def test_reset_between_profiles_clears_and_reloads_agreements(blank_db: Database, tmp_path: Path):
    load(blank_db, tmp_path)
    clock = fixture.reset(blank_db, fixture.read_profile(fixture.DEFAULT_PROFILE))
    assert clock.now == EPOCH
    assert [count(blank_db, t) for t in S5_TABLES] == [0, 0, 0, 0]
    fixture.reset(blank_db, load_fixture(write_profile(tmp_path)))
    assert [count(blank_db, t) for t in S5_TABLES] == [5, 4, 4, 0]
    assert rows(blank_db, "PRAGMA foreign_key_check") == []


def test_the_base_profile_loads_no_merchants_or_agreements(blank_db: Database):
    fixture.load_into_empty(blank_db, fixture.read_profile(fixture.DEFAULT_PROFILE))
    assert [count(blank_db, t) for t in S5_TABLES] == [0, 0, 0, 0]


def _mandate(doc: dict, ref: str, **changes) -> dict:
    next(m for m in doc["mandates"] if m["id"] == ref).update(changes)
    return doc


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: _mandate(d, "mdt_video", merchant="mer_unverified"), "not verified"),
        (lambda d: _mandate(d, "mdt_video", merchant="mer_nobody"), "unknown merchant"),
        (lambda d: _mandate(d, "mdt_video", account="acc_other"), "is not cus_main's"),
        (lambda d: _mandate(d, "mdt_video", account="acc_nothing"), "is not cus_main's"),
        (lambda d: _mandate(d, "mdt_video", customer="cus_nobody"), "unknown customer"),
        (lambda d: _mandate(d, "mdt_video", purpose_id="pur_sub_cloud"), "used twice"),
    ],
    ids=["unverified", "unknown merchant", "foreign account", "unknown account",
         "unknown customer", "shared purpose"],
)  # fmt: skip
def test_references_are_checked_and_nothing_is_loaded(
    blank_db: Database, tmp_path: Path, mutate, message: str
):
    doc = s5_doc()
    mutate(doc)
    with pytest.raises(FixtureError, match=message):
        load(blank_db, tmp_path, doc=doc)
    with blank_db.read() as conn:
        assert fixture.is_empty(conn)
    assert [count(blank_db, t) for t in S5_TABLES] == [0, 0, 0, 0]
