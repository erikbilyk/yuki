"""The direct-debit sweep (S5-SPEC §5.8 steps 1 and 3) through the app and the clock walk.

Judged on stored requests, actions, cards and audit rows. The sweep also runs after each
confirm and verify, at every clock step and at startup.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wg.contracts.enums import AuditActor
from wg.contracts.fixture import load_fixture
from wg.core import k5_mandates
from wg.k6 import fixture, mandates, plans
from wg.k6.clock import advance_clock
from wg.k6.db import Database
from wg.k6.simtime import SHANGHAI, to_db

from ..s5_profile import write_profile
from .conftest import Browser
from .s5_flows import confirm, draft, merchant_request, open_card, pay

pytestmark = pytest.mark.layer("H")

EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)


@pytest.fixture
def s5(browser: Browser, tmp_path: Path) -> Browser:
    fixture.reset(browser.db, load_fixture(write_profile(tmp_path)))
    browser.login("opr_demo")
    browser.login("cus_main")
    return browser


def sweep(browser: Browser) -> None:
    k5_mandates.sweep(browser.db, browser.client.app.state.trusted.step_up)


def set_clock(browser: Browser, to: datetime) -> None:
    """Move the simulated clock without the clock walk, so nothing sweeps on the way."""
    with browser.db.unit_of_work("test clock") as uow:
        advance_clock(uow, to)


def advance(browser: Browser, to: datetime) -> None:
    response = browser.sim("/sim/clock/advance", {"to": to.isoformat()})
    assert response.status_code == 200, response.text


def dump(db: Database) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table, for comparing whole database states."""
    with db.read() as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                " ORDER BY name"
            )
        ]
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in tables
        }


def status_of(browser: Browser, table: str, key: str, value: str) -> str:
    with browser.db.read() as conn:
        row = conn.execute(f"SELECT status FROM {table} WHERE {key} = ?", (value,)).fetchone()
    return row["status"]


def audit(browser: Browser, event: str) -> list[dict[str, Any]]:
    with browser.db.read() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT at, actor, ref, decision, reason FROM audit_log WHERE event = ?"
                " ORDER BY audit_id",
                (event,),
            )
        ]


# Step 1: unanswered requests


def test_a_long_clock_jump_stops_at_respond_by_and_records_the_expiry_there(s5: Browser):
    request = merchant_request(s5)
    respond_by = EPOCH + timedelta(hours=72)
    with s5.db.read() as conn:
        assert plans.next_due_instant(conn, EPOCH, EPOCH + timedelta(days=8)) == respond_by

    advance(s5, datetime(2026, 11, 10, 9, 0, tzinfo=SHANGHAI))

    assert status_of(s5, "debit_requests", "request_id", request["request_id"]) == "EXPIRED"
    (expired,) = audit(s5, "request_expired")
    assert (expired["actor"], expired["ref"], expired["decision"], expired["reason"]) == (
        AuditActor.COORDINATOR.value,
        request["request_id"],
        "EXPIRED",
        "EXPIRED",
    )
    assert datetime.fromisoformat(expired["at"]) == respond_by
    with s5.db.read() as conn:
        row = conn.execute(
            "SELECT reason_code, version FROM debit_requests WHERE request_id = ?",
            (request["request_id"],),
        ).fetchone()
    assert (row["reason_code"], row["version"]) == ("EXPIRED", 2)


def test_a_request_is_answerable_until_just_before_respond_by(s5: Browser):
    request = merchant_request(s5)
    respond_by = EPOCH + timedelta(hours=72)

    advance(s5, respond_by - timedelta(minutes=1))
    assert status_of(s5, "debit_requests", "request_id", request["request_id"]) == (
        "AWAITING_CLIENT"
    )
    advance(s5, respond_by)
    assert status_of(s5, "debit_requests", "request_id", request["request_id"]) == "EXPIRED"


def test_two_sweeps_equal_one(s5: Browser):
    late = merchant_request(s5, "mdt_video")
    pay_draft = draft(s5, late["request_id"])
    open_card(s5, pay_draft)
    set_clock(s5, EPOCH + timedelta(days=4))

    sweep(s5)
    once = dump(s5.db)
    sweep(s5)

    assert dump(s5.db) == once
    assert status_of(s5, "debit_requests", "request_id", late["request_id"]) == "EXPIRED"
    assert status_of(s5, "actions", "logical_action_id", pay_draft.logical_action_id) == (
        "CANCELLED"
    )
    assert len(audit(s5, "request_expired")) == 1
    assert [row["decision"] for row in audit(s5, "action_cancelled")] == ["request_closed"]


def test_without_direct_debit_rows_the_sweep_writes_nothing(browser: Browser):
    before = dump(browser.db)
    sweep(browser)
    advance(browser, EPOCH + timedelta(minutes=1))
    after = dump(browser.db)

    moved = {name for name in before if before[name] != after[name]}
    # The clock move itself writes its own rows; the sweep adds none.
    assert moved <= {"sim_clock", "audit_log", "domain_events", "event_cursors"}
    new_events = {row[3] for row in after["audit_log"][len(before["audit_log"]) :]}
    assert new_events == {"clock_advanced"}


# Step 3: stale decision actions


def test_a_stale_pay_card_expires_when_its_request_is_declined(s5: Browser):
    request = merchant_request(s5)
    pay_draft = draft(s5, request["request_id"])
    pay_card = open_card(s5, pay_draft)
    decline_card = open_card(s5, draft(s5, request["request_id"], "decline"))

    assert confirm(s5, decline_card).status_code == 201

    assert status_of(s5, "debit_requests", "request_id", request["request_id"]) == "DECLINED"
    assert status_of(s5, "actions", "logical_action_id", pay_draft.logical_action_id) == (
        "CANCELLED"
    )
    assert s5.get(f"/v1/cards/{pay_card.card_id}").json()["status"] == "expired"
    refused = confirm(s5, pay_card)
    assert (refused.status_code, refused.json()["code"]) == (409, "APPROVAL_EXPIRED")
    with s5.db.read() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM operations WHERE kind = 'DEBIT_PAY'").fetchone()[0]
            == 0
        )


def test_a_pay_card_of_an_older_agreement_version_is_closed_at_the_next_sweep(s5: Browser):
    request = merchant_request(s5)
    stale = draft(s5, request["request_id"])
    stale_card = open_card(s5, stale)
    with s5.db.unit_of_work("test cap change") as uow:
        mandate = mandates.find_mandate(uow, "mdt_video")
        assert mandate is not None
        mandates.update_mandate(
            uow, mandate, actor=AuditActor.CUSTOMER, reason="test", cap_fen=2_800
        )

    advance(s5, EPOCH + timedelta(minutes=1))

    assert status_of(s5, "actions", "logical_action_id", stale.logical_action_id) == "CANCELLED"
    assert [row["decision"] for row in audit(s5, "action_cancelled")] == ["mandate_changed"]
    refused = confirm(s5, stale_card)
    assert (refused.status_code, refused.json()["code"]) == (409, "APPROVAL_EXPIRED")
    # The request still waits; a new card carries the new version and pays.
    body = pay(s5, request["request_id"])
    assert body["operation"]["status"] == "SETTLED"


def test_the_sweep_after_a_confirm_covers_only_that_customer(s5: Browser):
    other = s5.sim(
        "/sim/debit-requests",
        {
            "merchant_id": "mer_video",
            "merchant_request_id": "other-late",
            "mandate_ref": "mdt_other_video",
            "amount_fen": 2_500,
            "currency": "CNY",
        },
    ).json()
    own = merchant_request(s5, "mdt_cloud", 2_100)
    with s5.db.unit_of_work("test answer window") as uow:
        uow.execute(
            "UPDATE debit_requests SET respond_by = ? WHERE request_id = ?",
            (to_db(EPOCH + timedelta(hours=80)), own["request_id"]),
        )
    # Past the other customer's deadline, without the clock walk's own sweep.
    set_clock(s5, EPOCH + timedelta(hours=72))

    pay(s5, own["request_id"])

    assert status_of(s5, "debit_requests", "request_id", other["request_id"]) == ("AWAITING_CLIENT")
