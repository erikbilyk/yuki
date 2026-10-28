from collections import Counter
from pathlib import Path

import pytest

from wg.contracts.api import (
    AuditView,
    Card,
    CardStatus,
    ChallengeView,
    ConfirmResult,
    SmsView,
    VerifyResult,
)
from wg.contracts.enums import ActionStatus, Role, Tier
from wg.contracts.errors import ErrorBody, ErrorCode

from ..core.drafts import MAIN, OTHER, RED_FEN, YELLOW_FEN, current, transfer_card
from .conftest import Browser, make_client

pytestmark = pytest.mark.layer("H")


def card_of(browser: Browser, amount_fen: int, customer_id: str = MAIN) -> Card:
    _, row = transfer_card(browser.db, amount_fen, customer_id=customer_id)
    response = browser.get(f"/v1/cards/{row.card_id}")
    assert response.status_code == 200, response.text
    return Card.model_validate(response.json())


def confirm(browser: Browser, card: Card, terms_hash: str | None = None, **extra):
    body = {"terms_hash": terms_hash or card.terms_hash, **extra}
    return browser.post(f"/v1/cards/{card.card_id}/confirm", body)


def sms_code(browser: Browser, challenge_id: str) -> str:
    view = SmsView.model_validate(browser.get("/v1/sms").json())
    codes = [m.code for m in view.messages if m.challenge_id == challenge_id]
    assert len(codes) == 1
    return codes[0]


def audit_lines(browser: Browser) -> Counter[str]:
    view = AuditView.model_validate(browser.get("/v1/audit").json())
    return Counter(line.model_dump_json() for line in view.lines)


def new_lines(browser: Browser, before: Counter[str]) -> list[str]:
    return list((audit_lines(browser) - before).elements())


def test_yellow_card_confirms_and_executes(browser: Browser):
    card = card_of(browser, YELLOW_FEN)
    assert card.status is CardStatus.OPEN and card.tier is Tier.YELLOW and not card.requires_code
    response = confirm(browser, card)
    assert response.status_code == 201
    result = ConfirmResult.model_validate(response.json())
    assert result.action_status is ActionStatus.SETTLED and result.challenge is None
    assert result.operation is not None and result.operation.amount_fen == YELLOW_FEN
    after = Card.model_validate(browser.get(f"/v1/cards/{card.card_id}").json())
    assert after.status is CardStatus.CONFIRMED and after.approval_id == result.approval_id
    repeat = confirm(browser, card)
    assert repeat.status_code == 200
    repeated = ConfirmResult.model_validate(repeat.json())
    assert repeated.approval_id == result.approval_id
    assert repeated.operation == result.operation


@pytest.mark.case("T7")
def test_red_card_needs_the_code_from_the_sms_panel(browser: Browser):
    card = card_of(browser, RED_FEN)
    assert card.requires_code
    result = ConfirmResult.model_validate(confirm(browser, card).json())
    assert result.action_status is ActionStatus.AWAITING_STEPUP
    challenge = result.challenge
    assert challenge is not None and challenge.attempts_left == 3
    code = sms_code(browser, challenge.challenge_id)
    sms = SmsView.model_validate(browser.get("/v1/sms").json()).messages[0]
    assert sms.amount_fen == RED_FEN and sms.logical_action_id == card.logical_action_id

    response = browser.post(f"/v1/challenges/{challenge.challenge_id}/verify", {"code": code})
    assert response.status_code == 200, response.text
    verified = VerifyResult.model_validate(response.json())
    assert verified.action_status is ActionStatus.SETTLED
    assert verified.operation is not None and verified.operation.amount_fen == RED_FEN
    assert SmsView.model_validate(browser.get("/v1/sms").json()).messages == ()
    with browser.db.read() as conn:
        stored = conn.execute(
            "SELECT code FROM sms_outbox WHERE challenge_id = ?", (challenge.challenge_id,)
        ).fetchone()[0]
    assert stored is None


@pytest.mark.case("T7")
def test_expired_code_is_refused_and_wiped(browser: Browser):
    card = card_of(browser, RED_FEN)
    challenge = ConfirmResult.model_validate(confirm(browser, card).json()).challenge
    assert challenge is not None
    code = sms_code(browser, challenge.challenge_id)
    assert browser.sim("/sim/clock/advance", {"minutes": 5}).status_code == 200
    assert SmsView.model_validate(browser.get("/v1/sms").json()).messages == ()
    response = browser.post(f"/v1/challenges/{challenge.challenge_id}/verify", {"code": code})
    assert response.status_code == 409 and response.json()["code"] == "CODE_EXPIRED"

    reissued = browser.post(f"/v1/challenges/{challenge.challenge_id}/reissue")
    assert reissued.status_code == 201
    fresh = ChallengeView.model_validate(reissued.json())
    new_code = sms_code(browser, fresh.challenge_id)
    response = browser.post(f"/v1/challenges/{fresh.challenge_id}/verify", {"code": new_code})
    assert response.status_code == 200


@pytest.mark.case("T50")
def test_wrong_code_is_counted_and_reported(browser: Browser):
    card = card_of(browser, RED_FEN)
    challenge = ConfirmResult.model_validate(confirm(browser, card).json()).challenge
    assert challenge is not None
    code = sms_code(browser, challenge.challenge_id)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    response = browser.post(f"/v1/challenges/{challenge.challenge_id}/verify", {"code": wrong})
    assert response.status_code == 422
    assert response.json()["code"] == "CODE_INVALID"
    assert response.json()["detail"]["attempts_left"] == 2


@pytest.mark.case("T9")
def test_forged_terms_hash_is_refused_and_audited(browser: Browser):
    card = card_of(browser, YELLOW_FEN)
    before = audit_lines(browser)
    response = confirm(browser, card, terms_hash="0" * 64)
    assert response.status_code == 409 and response.json()["code"] == "TERMS_MISMATCH"
    lines = new_lines(browser, before)
    assert lines and any("TERMS_MISMATCH" in line for line in lines)
    after = Card.model_validate(browser.get(f"/v1/cards/{card.card_id}").json())
    assert after.status is CardStatus.OPEN


@pytest.mark.case("T9")
def test_unknown_body_field_is_refused_and_audited(browser: Browser):
    card = card_of(browser, YELLOW_FEN)
    before = audit_lines(browser)
    response = confirm(browser, card, user_confirmed=True)
    assert response.status_code == 422
    body = ErrorBody.model_validate(response.json())
    assert body.code is ErrorCode.UNSUPPORTED_REQUEST and body.detail == {
        "fields": ["body.user_confirmed"]
    }
    assert new_lines(browser, before)
    assert current(browser.db, card.logical_action_id).status is ActionStatus.AWAITING_APPROVAL


@pytest.mark.case("T9")
def test_confirm_without_csrf_is_refused_and_audited(browser: Browser):
    card = card_of(browser, YELLOW_FEN)
    before = audit_lines(browser)
    path = f"/v1/cards/{card.card_id}/confirm"
    missing = browser.post(path, {"terms_hash": card.terms_hash}, csrf=False)
    other_role = browser.post(path, {"terms_hash": card.terms_hash}, role=Role.DEMO)
    for response in (missing, other_role):
        assert response.status_code == 403 and response.json()["code"] == "CSRF_FAILED"
    assert len(new_lines(browser, before)) == 2
    assert current(browser.db, card.logical_action_id).status is ActionStatus.AWAITING_APPROVAL


@pytest.mark.case("T9")
def test_challenge_without_approval_is_refused_and_audited(browser: Browser):
    card = card_of(browser, RED_FEN)
    before = audit_lines(browser)
    response = browser.post(f"/v1/actions/{card.logical_action_id}/challenge")
    assert response.status_code == 409 and response.json()["code"] == "APPROVAL_REQUIRED"
    assert any("APPROVAL_REQUIRED" in line for line in new_lines(browser, before))
    assert SmsView.model_validate(browser.get("/v1/sms").json()).messages == ()


@pytest.mark.case("T9")
def test_customer_requests_a_code_for_an_action_awaiting_step_up(browser: Browser):
    card = card_of(browser, RED_FEN)
    first = ConfirmResult.model_validate(confirm(browser, card).json()).challenge
    assert first is not None
    response = browser.post(f"/v1/actions/{card.logical_action_id}/challenge")
    assert response.status_code == 201
    second = ChallengeView.model_validate(response.json())
    messages = SmsView.model_validate(browser.get("/v1/sms").json()).messages
    assert [m.challenge_id for m in messages] == [second.challenge_id]


@pytest.mark.case("T54")
def test_foreign_and_missing_objects_get_identical_404_bodies(browser: Browser):
    foreign = transfer_card(browser.db, YELLOW_FEN, customer_id=OTHER, account_id="acc_other")[1]
    pairs = [
        ("GET", f"/v1/cards/{foreign.card_id}", "/v1/cards/crd_missing"),
        (
            "POST",
            f"/v1/cards/{foreign.card_id}/confirm",
            "/v1/cards/crd_missing/confirm",
        ),
        (
            "POST",
            f"/v1/actions/{foreign.logical_action_id}/challenge",
            "/v1/actions/act_missing/challenge",
        ),
    ]
    before = audit_lines(browser)
    for method, foreign_path, missing_path in pairs:
        body = {"terms_hash": foreign.terms_hash} if "confirm" in foreign_path else None
        if method == "GET":
            responses = [browser.get(foreign_path), browser.get(missing_path)]
        else:
            responses = [browser.post(foreign_path, body), browser.post(missing_path, body)]
        assert [r.status_code for r in responses] == [404, 404]
        assert responses[0].content == responses[1].content
    assert len(new_lines(browser, before)) == 2 * len(pairs)
    with browser.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_sms_panel_shows_only_the_sessions_customer(browser: Browser):
    _, other_card = transfer_card(browser.db, RED_FEN, customer_id=OTHER, account_id="acc_other")
    other = Browser(browser.client)
    other.client.cookies.delete("wg_session")
    other.login("cus_other")
    response = other.post(
        f"/v1/cards/{other_card.card_id}/confirm", {"terms_hash": other_card.terms_hash}
    )
    assert response.status_code == 201
    assert len(SmsView.model_validate(other.get("/v1/sms").json()).messages) == 1
    browser.client.cookies.delete("wg_session")
    browser.login("cus_main")
    assert SmsView.model_validate(browser.get("/v1/sms").json()).messages == ()


@pytest.mark.case("T9")
def test_trusted_routes_refuse_staff_sessions(tmp_path: Path):
    with make_client(tmp_path / "wg.db") as client:
        staff = Browser(client)
        staff.login("opr_ops")
        staff.login("opr_demo")
        response = staff.post("/v1/cards/crd_x/confirm", {"terms_hash": "0" * 64}, role=Role.DEMO)
        assert response.status_code == 401 and response.json()["code"] == "SESSION_EXPIRED"
        assert staff.post("/v1/actions/act_x/challenge", role=Role.OPERATOR).status_code == 401
        assert staff.get("/v1/sms").status_code == 401
