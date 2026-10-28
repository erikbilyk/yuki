import json
import sqlite3
from datetime import timedelta

import pytest

from wg.contracts.api import CardStatus
from wg.contracts.enums import ActionStatus, AuditActor, LockScope, Tier
from wg.contracts.errors import ErrorCode, WgError
from wg.core import k4_approval as k4
from wg.core.hashes import sha256_hex
from wg.core.k4_approval import InvalidationReason, Refusal, Verified
from wg.k6 import approvals, challenges, locks
from wg.k6.actions import IllegalTransition, set_status
from wg.k6.clock import advance_clock
from wg.k6.db import Database
from wg.k6.simtime import now

from .drafts import (
    MAIN,
    OTHER,
    RED_FEN,
    STEP_UP,
    WANGFANG,
    YELLOW_FEN,
    advance,
    clock,
    current,
    draft_transfer,
    open_card,
    transfer_card,
)

pytestmark = pytest.mark.layer("D")


def confirm(db: Database, card_id: str, terms_hash: str, customer_id: str = MAIN):
    return db.run_in_uow(
        lambda uow: k4.confirm_card(
            uow,
            STEP_UP,
            customer_id=customer_id,
            card_id=card_id,
            presented_terms_hash=terms_hash,
            now=now(uow),
        ),
        "test confirm",
    )


def verify(db: Database, challenge_id: str, code: str, customer_id: str = MAIN):
    return db.run_in_uow(
        lambda uow: k4.verify_code(
            uow,
            STEP_UP,
            customer_id=customer_id,
            challenge_id=challenge_id,
            code=code,
            now=now(uow),
        ),
        "test verify",
    )


def sms_code(db: Database, challenge_id: str) -> str | None:
    with db.read() as conn:
        row = conn.execute(
            "SELECT code FROM sms_outbox WHERE challenge_id = ?", (challenge_id,)
        ).fetchone()
    return row["code"]


def challenge(db: Database, challenge_id: str):
    with db.read() as conn:
        return challenges.get_challenge(conn, challenge_id)


def red_challenge(db: Database, amount_fen: int = RED_FEN, **options) -> tuple[str, str, str]:
    """A RED transfer confirmed on its card: (action id, challenge id, code)."""
    action, card = transfer_card(db, amount_fen, **options)
    done = confirm(db, card.card_id, card.terms_hash)
    assert done.challenge is not None
    code = sms_code(db, done.challenge.challenge_id)
    assert code is not None
    return action.logical_action_id, done.challenge.challenge_id, code


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


def consume(db: Database, logical_action_id: str):
    return db.run_in_uow(
        lambda uow: k4.consume_grant(uow, current(db, logical_action_id), now(uow)),
        "test consume",
    )


def refusal_code(outcome) -> ErrorCode:
    assert isinstance(outcome, Refusal), outcome
    return outcome.code


# Cards and approvals


def test_card_binds_the_terms_and_moves_the_draft_to_awaiting_approval(db: Database):
    action, card = transfer_card(db, YELLOW_FEN)
    assert current(db, action.logical_action_id).status is ActionStatus.AWAITING_APPROVAL
    assert card.tier is Tier.YELLOW
    assert card.terms["params"] == {
        "account_id": "acc_main",
        "amount_fen": YELLOW_FEN,
        "payee_id": "pay_mom",
    }
    assert card.terms_hash == sha256_hex(dict(card.terms))
    # The card holds until the end of the simulated day.
    assert card.expires_at.isoformat() == "2026-11-03T00:00:00+08:00"
    again = open_card(db, current(db, action.logical_action_id))
    assert again.card_id == card.card_id


def test_card_view_shows_status_tier_and_code_requirement(db: Database):
    _, card = transfer_card(db, RED_FEN)
    with db.read() as conn:
        view = k4.card_view(conn, card, clock(db))
    assert view.status is CardStatus.OPEN and view.tier is Tier.RED and view.requires_code
    assert view.lines[0].text == "¥1500.00"


@pytest.mark.case("T5")
def test_yellow_confirm_makes_the_action_ready_without_a_code(db: Database):
    action, card = transfer_card(db, 100_000)
    done = confirm(db, card.card_id, card.terms_hash)
    assert done.action.status is ActionStatus.READY and done.challenge is None
    assert done.approval.terms_hash == card.terms_hash
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM challenges").fetchone()[0] == 0
        assert k4.valid_approval(conn, current(db, action.logical_action_id), clock(db))


@pytest.mark.case("T5")
def test_red_confirm_sends_a_code_and_waits_for_step_up(db: Database):
    action_id, challenge_id, code = red_challenge(db)
    assert current(db, action_id).status is ActionStatus.AWAITING_STEPUP
    issued = challenge(db, challenge_id)
    assert issued.expires_at - issued.issued_at == timedelta(minutes=5)
    assert issued.max_attempts == 3 and issued.attempts == 0
    assert code is not None and len(code) == 6 and code.isdigit()
    # Only the HMAC is stored with the challenge.
    assert code not in issued.code_hmac and len(issued.code_hmac) == 64


def test_forged_terms_hash_is_refused_and_changes_nothing(db: Database):
    action, card = transfer_card(db, YELLOW_FEN)
    with pytest.raises(WgError) as refused:
        confirm(db, card.card_id, "0" * 64)
    assert refused.value.code is ErrorCode.TERMS_MISMATCH
    assert current(db, action.logical_action_id).status is ActionStatus.AWAITING_APPROVAL
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_terms_changed_under_the_card_are_refused(db: Database):
    action, card = transfer_card(db, YELLOW_FEN)
    with db.unit_of_work("tamper") as uow:
        params = json.dumps({"account_id": "acc_main", "amount_fen": 1, "payee_id": "pay_mom"})
        uow.execute(
            "UPDATE actions SET params_json = ? WHERE logical_action_id = ?",
            (params, action.logical_action_id),
        )
    with pytest.raises(WgError) as refused:
        confirm(db, card.card_id, card.terms_hash)
    assert refused.value.code is ErrorCode.TERMS_MISMATCH


def test_repeated_confirm_creates_nothing_new(db: Database):
    _, card = transfer_card(db, RED_FEN)
    first = confirm(db, card.card_id, card.terms_hash)
    again = confirm(db, card.card_id, card.terms_hash)
    assert again.repeated and again.approval.approval_id == first.approval.approval_id
    assert again.challenge.challenge_id == first.challenge.challenge_id
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM challenges").fetchone()[0] == 1


def test_card_expires_at_the_end_of_the_day(db: Database):
    _, card = transfer_card(db, YELLOW_FEN)
    advance(db, timedelta(hours=14, minutes=59, seconds=59))
    with db.read() as conn:
        assert k4.card_status(conn, card, clock(db)) is CardStatus.OPEN
    advance(db, timedelta(seconds=1))
    with pytest.raises(WgError) as refused:
        confirm(db, card.card_id, card.terms_hash)
    assert refused.value.code is ErrorCode.APPROVAL_EXPIRED


def test_confirm_under_a_lock_is_refused(db: Database):
    _, card = transfer_card(db, YELLOW_FEN)
    with db.unit_of_work("lock") as uow:
        locks.set_lock(uow, LockScope.CUSTOMER, MAIN, "test")
    with pytest.raises(WgError) as refused:
        confirm(db, card.card_id, card.terms_hash)
    assert refused.value.code is ErrorCode.LOCKED_WRITE


@pytest.mark.case("T54")
def test_foreign_and_missing_cards_get_the_same_refusal(db: Database):
    _, card = transfer_card(db, YELLOW_FEN)
    errors = []
    for card_id in (card.card_id, "crd_doesnotexist"):
        with pytest.raises(WgError) as refused:
            confirm(db, card_id, card.terms_hash, customer_id=OTHER)
        errors.append((refused.value.code, refused.value.detail))
    assert errors == [(ErrorCode.NOT_FOUND, {}), (ErrorCode.NOT_FOUND, {})]


def test_approvals_are_immutable_and_revoked_once(db: Database):
    _, card = transfer_card(db, YELLOW_FEN)
    approval = confirm(db, card.card_id, card.terms_hash).approval
    with pytest.raises(sqlite3.IntegrityError):
        with db.unit_of_work("tamper") as uow:
            uow.execute(
                "UPDATE approvals SET terms_hash = ? WHERE approval_id = ?",
                ("1" * 64, approval.approval_id),
            )
    with pytest.raises(sqlite3.IntegrityError):
        with db.unit_of_work("tamper") as uow:
            uow.execute("UPDATE cards SET approval_id = NULL WHERE card_id = ?", (card.card_id,))
    at = clock(db)
    with db.unit_of_work("revoke") as uow:
        assert approvals.revoke_approval(uow, approval.approval_id, at)
        assert not approvals.revoke_approval(uow, approval.approval_id, at)
    with pytest.raises(sqlite3.IntegrityError):
        with db.unit_of_work("tamper") as uow:
            uow.execute(
                "UPDATE approvals SET revoked_at = NULL WHERE approval_id = ?",
                (approval.approval_id,),
            )


def test_transitions_follow_the_state_machine(db: Database):
    action = draft_transfer(db, YELLOW_FEN)
    with pytest.raises(IllegalTransition):
        with db.unit_of_work("bad move") as uow:
            set_status(uow, action, ActionStatus.SUBMITTED)
    with pytest.raises(IllegalTransition):
        with db.unit_of_work("stale row") as uow:
            set_status(uow, action, ActionStatus.AWAITING_APPROVAL)
            set_status(uow, action, ActionStatus.CANCELLED)
    assert current(db, action.logical_action_id).status is ActionStatus.DRAFT


# Challenges (SPEC §9.3, R5, T7, T50)


@pytest.mark.case("R5")
def test_code_is_valid_at_four_minutes_fifty_nine(db: Database):
    action_id, challenge_id, code = red_challenge(db)
    advance(db, timedelta(minutes=4, seconds=59))
    outcome = verify(db, challenge_id, code)
    assert isinstance(outcome, Verified) and not outcome.repeated
    assert current(db, action_id).status is ActionStatus.READY
    assert sms_code(db, challenge_id) is None
    assert challenge(db, challenge_id).is_grant


@pytest.mark.case("R5")
def test_code_expires_at_five_minutes(db: Database):
    action_id, challenge_id, code = red_challenge(db)
    with db.unit_of_work("clock only") as uow:
        # Move the clock without the expiry sweep, so verify meets the boundary itself.
        advance_clock(uow, now(uow) + timedelta(minutes=5))
    assert refusal_code(verify(db, challenge_id, code)) is ErrorCode.CODE_EXPIRED
    closed = challenge(db, challenge_id)
    assert closed.invalidation_reason == InvalidationReason.EXPIRED
    assert sms_code(db, challenge_id) is None
    assert current(db, action_id).status is ActionStatus.AWAITING_STEPUP
    assert refusal_code(verify(db, challenge_id, code)) is ErrorCode.CODE_EXPIRED


def test_clock_advance_wipes_expired_codes(db: Database):
    _, challenge_id, _ = red_challenge(db)
    advance(db, timedelta(minutes=5))
    assert sms_code(db, challenge_id) is None
    assert challenge(db, challenge_id).invalidation_reason == InvalidationReason.EXPIRED


@pytest.mark.case("T50")
def test_three_wrong_codes_close_the_challenge(db: Database):
    action_id, challenge_id, code = red_challenge(db)
    first = verify(db, challenge_id, wrong(code))
    assert refusal_code(first) is ErrorCode.CODE_INVALID and first.detail["attempts_left"] == 2
    assert refusal_code(verify(db, challenge_id, wrong(code))) is ErrorCode.CODE_INVALID
    last = verify(db, challenge_id, wrong(code))
    assert refusal_code(last) is ErrorCode.CODE_ATTEMPTS_EXHAUSTED
    assert refusal_code(verify(db, challenge_id, code)) is ErrorCode.CODE_ATTEMPTS_EXHAUSTED
    closed = challenge(db, challenge_id)
    assert closed.attempts == 3 and closed.invalidation_reason == "attempts_exhausted"
    assert sms_code(db, challenge_id) is None
    assert current(db, action_id).status is ActionStatus.AWAITING_STEPUP


def test_correct_code_on_the_third_attempt(db: Database):
    action_id, challenge_id, code = red_challenge(db)
    verify(db, challenge_id, wrong(code))
    verify(db, challenge_id, wrong(code))
    assert isinstance(verify(db, challenge_id, code), Verified)
    assert current(db, action_id).status is ActionStatus.READY


@pytest.mark.case("T7")
def test_code_of_another_action_does_not_verify(db: Database):
    _, mom_challenge, mom_code = red_challenge(db)
    _, other_challenge, other_code = red_challenge(db, payee_id=WANGFANG)
    if other_code == mom_code:
        pytest.skip("both random codes are equal")
    assert refusal_code(verify(db, mom_challenge, other_code)) is ErrorCode.CODE_INVALID
    assert isinstance(verify(db, mom_challenge, mom_code), Verified)
    assert challenge(db, other_challenge).is_open


@pytest.mark.case("T50")
def test_scope_mismatch_closes_the_challenge(db: Database):
    action_id, challenge_id, code = red_challenge(db)
    with db.unit_of_work("tamper") as uow:
        params = json.dumps(
            {"account_id": "acc_main", "amount_fen": 999_999, "payee_id": "pay_mom"}
        )
        uow.execute(
            "UPDATE actions SET params_json = ? WHERE logical_action_id = ?", (params, action_id)
        )
    assert refusal_code(verify(db, challenge_id, code)) is ErrorCode.CODE_SCOPE_MISMATCH
    assert challenge(db, challenge_id).invalidation_reason == "scope_mismatch"


def test_foreign_challenge_is_not_found(db: Database):
    _, challenge_id, code = red_challenge(db)
    with pytest.raises(WgError) as refused:
        verify(db, challenge_id, code, customer_id=OTHER)
    assert refused.value.code is ErrorCode.NOT_FOUND and refused.value.detail == {}
    assert challenge(db, challenge_id).attempts == 0


@pytest.mark.case("T9")
def test_challenge_needs_an_approval(db: Database):
    action = draft_transfer(db, RED_FEN)
    with pytest.raises(WgError) as refused:
        db.run_in_uow(
            lambda uow: k4.request_challenge(
                uow,
                STEP_UP,
                customer_id=MAIN,
                logical_action_id=action.logical_action_id,
                now=now(uow),
            ),
            "test challenge",
        )
    assert refused.value.code is ErrorCode.APPROVAL_REQUIRED
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM challenges").fetchone()[0] == 0


def test_requesting_a_code_again_closes_the_previous_one(db: Database):
    action_id, first_id, first_code = red_challenge(db)
    second = db.run_in_uow(
        lambda uow: k4.request_challenge(
            uow, STEP_UP, customer_id=MAIN, logical_action_id=action_id, now=now(uow)
        ),
        "test challenge",
    )
    assert challenge(db, first_id).invalidation_reason == "reissued"
    assert sms_code(db, first_id) is None
    assert refusal_code(verify(db, first_id, first_code)) is ErrorCode.CODE_INVALID
    assert isinstance(verify(db, second.challenge_id, sms_code(db, second.challenge_id)), Verified)


@pytest.mark.case("T7")
def test_reissue_after_expiry_gives_a_working_code(db: Database):
    action_id, challenge_id, _ = red_challenge(db)
    advance(db, timedelta(minutes=5))
    fresh = db.run_in_uow(
        lambda uow: k4.reissue_challenge(
            uow, STEP_UP, customer_id=MAIN, challenge_id=challenge_id, now=now(uow)
        ),
        "test reissue",
    )
    assert fresh.challenge_id != challenge_id and fresh.logical_action_id == action_id
    assert isinstance(verify(db, fresh.challenge_id, sms_code(db, fresh.challenge_id)), Verified)


def test_verified_code_repeats_without_a_second_effect(db: Database):
    _, challenge_id, code = red_challenge(db)
    verify(db, challenge_id, code)
    again = verify(db, challenge_id, code)
    assert isinstance(again, Verified) and again.repeated


def test_refusals_leave_audit_rows(db: Database):
    _, challenge_id, code = red_challenge(db)
    verify(db, challenge_id, wrong(code))
    with db.read() as conn:
        row = conn.execute(
            "SELECT actor, decision, customer_id FROM audit_log WHERE event = 'verify_refused'"
        ).fetchone()
    assert tuple(row) == (AuditActor.CUSTOMER.value, "CODE_INVALID", MAIN)


# Grants (SPEC §9.3, §12.3)


def test_grant_is_consumed_once(db: Database):
    action_id, challenge_id, code = red_challenge(db)
    verify(db, challenge_id, code)
    used = consume(db, action_id)
    assert used.challenge_id == challenge_id and used.consumed_at is not None
    with pytest.raises(WgError) as again:
        consume(db, action_id)
    assert again.value.code is ErrorCode.GRANT_CONSUMED
    assert refusal_code(verify(db, challenge_id, code)) is ErrorCode.GRANT_CONSUMED
    with pytest.raises(sqlite3.IntegrityError):
        with db.unit_of_work("tamper") as uow:
            uow.execute(
                "UPDATE challenges SET consumed_at = NULL WHERE challenge_id = ?", (challenge_id,)
            )


def test_no_grant_without_a_verified_code(db: Database):
    action_id, _, _ = red_challenge(db)
    with pytest.raises(WgError) as refused:
        consume(db, action_id)
    assert refused.value.code is ErrorCode.APPROVAL_REQUIRED


def test_grant_expires_with_its_code(db: Database):
    action_id, challenge_id, code = red_challenge(db)
    advance(db, timedelta(minutes=2))
    verify(db, challenge_id, code)
    advance(db, timedelta(minutes=3))
    with pytest.raises(WgError) as refused:
        consume(db, action_id)
    assert refused.value.code is ErrorCode.CODE_EXPIRED


def test_grant_with_a_changed_scope_is_an_integrity_violation(db: Database):
    action_id, challenge_id, code = red_challenge(db)
    verify(db, challenge_id, code)
    with db.unit_of_work("tamper") as uow:
        params = json.dumps(
            {"account_id": "acc_main", "amount_fen": 999_999, "payee_id": "pay_mom"}
        )
        uow.execute(
            "UPDATE actions SET params_json = ? WHERE logical_action_id = ?", (params, action_id)
        )
    with pytest.raises(WgError) as refused:
        consume(db, action_id)
    assert refused.value.code is ErrorCode.INTEGRITY_VIOLATION
    assert challenge(db, challenge_id).consumed_at is None


def test_revoked_approval_closes_its_challenges(db: Database):
    action_id, challenge_id, code = red_challenge(db)
    approval_id = challenge(db, challenge_id).approval_id
    db.run_in_uow(
        lambda uow: k4.revoke_approval(uow, STEP_UP, approval_id, now(uow), AuditActor.CUSTOMER),
        "test revoke",
    )
    assert challenge(db, challenge_id).invalidation_reason == "approval_revoked"
    assert refusal_code(verify(db, challenge_id, code)) is ErrorCode.APPROVAL_REVOKED
    with pytest.raises(WgError) as refused:
        db.run_in_uow(
            lambda uow: k4.request_challenge(
                uow, STEP_UP, customer_id=MAIN, logical_action_id=action_id, now=now(uow)
            ),
            "test challenge",
        )
    assert refused.value.code is ErrorCode.APPROVAL_REVOKED


def test_lock_clearance_invalidates_grants_and_waits_for_a_new_code(db: Database):
    action_id, challenge_id, code = red_challenge(db)
    verify(db, challenge_id, code)
    assert current(db, action_id).status is ActionStatus.READY
    closed = db.run_in_uow(
        lambda uow: k4.invalidate_grants(
            uow, STEP_UP, InvalidationReason.LOCK_CLEARED, now(uow), customer_id=MAIN
        ),
        "test unlock",
    )
    assert [c.challenge_id for c in closed] == [challenge_id]
    assert current(db, action_id).status is ActionStatus.AWAITING_STEPUP
    with pytest.raises(WgError) as refused:
        consume(db, action_id)
    assert refused.value.code is ErrorCode.APPROVAL_REQUIRED


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (ActionStatus.CANCELLED, ErrorCode.APPROVAL_EXPIRED),
        (ActionStatus.PAUSED, ErrorCode.APPROVAL_EXPIRED),
        (ActionStatus.SUPERSEDED, ErrorCode.SUPERSEDED),
    ],
)
def test_a_closed_draft_gets_no_card(db: Database, status: ActionStatus, code: ErrorCode):
    action = draft_transfer(db, YELLOW_FEN)
    with db.unit_of_work("close") as uow:
        set_status(
            uow,
            action,
            status,
            superseded_by=2 if status is ActionStatus.SUPERSEDED else None,
            pause_reasons=["DATA_INCOMPLETE"] if status is ActionStatus.PAUSED else None,
        )
    with pytest.raises(WgError) as refused:
        open_card(db, current(db, action.logical_action_id))
    assert refused.value.code is code and refused.value.http_status == 409
