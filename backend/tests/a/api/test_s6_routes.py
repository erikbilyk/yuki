"""S6 over HTTP in process: composition, plan approval with the reserve, continuation, expiry."""

import json
from datetime import date, datetime, timedelta

import pytest

from wg.contracts.api import (
    CandidatesView,
    Card,
    CardKind,
    CardStatus,
    ConfirmResult,
    DraftResult,
    PlanDraftResult,
    PlansView,
    PlanView,
    SmsView,
    VerifyResult,
)
from wg.contracts.enums import ActionKind, ActionStatus, CheckpointResult, PlanKind, PlanStatus
from wg.contracts.errors import ErrorBody, ErrorCode
from wg.contracts.facts import FactConcept
from wg.core.k7_verify import collect_facts
from wg.k6.simtime import SHANGHAI
from wg.textcheck.claims import FEASIBLE_WITH_APPROVAL, check_claims

from .conftest import Browser

pytestmark = pytest.mark.layer("H")

EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)
SEARCH = {"sector": "technology", "target_max_pct": "40.0000", "fee_cap_fen": 2_000}
BOUNDS = {
    "p0_fen": 498_500,
    "spare0_fen": 100_000,
    "max_added_fen": 120_000,
    "max_retained_fen": 20_000,
    "min_purchase_fen": 478_500,
}


def ok(response, model, status=(200, 201)):
    assert response.status_code in status, response.text
    return model.model_validate(response.json())


def refused(response, code: ErrorCode) -> ErrorBody:
    assert 400 <= response.status_code < 500, response.text
    body = ErrorBody.model_validate(response.json())
    assert body.code is code, body
    return body


def at(day: int, hour: int = 9, minute: int = 0) -> datetime:
    return EPOCH.replace(hour=hour, minute=minute) + timedelta(days=day)


def advance(browser: Browser, moment: datetime) -> None:
    response = browser.sim("/sim/clock/advance", {"to": moment.isoformat()})
    assert response.status_code == 200, response.text


def rows(browser: Browser, sql: str, *params):
    with browser.db.read() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def plan_view(browser: Browser, plan_id: str) -> PlanView:
    return ok(browser.get(f"/v1/plans/{plan_id}"), PlanView, (200,))


def node(view: PlanView, kind: ActionKind, commitment_id: str | None = None):
    return next(
        n
        for n in view.nodes
        if n.kind is kind and (commitment_id is None or n.commitment_id == commitment_id)
    )


def draft(browser: Browser, path: str, body: dict) -> DraftResult:
    return ok(browser.post(f"/v1/drafts/{path}", body), DraftResult, (201,))


def fund_drafts(browser: Browser, with_bounds: bool = True) -> tuple[str, dict[ActionKind, str]]:
    found = ok(browser.post("/v1/funds/candidates", SEARCH), CandidatesView, (200,))
    body = {"calc_result_id": found.calc_result_id, "with_bounds": with_bounds}
    funds = ok(browser.post("/v1/funds/plans", body), PlanDraftResult, (201,))
    assert funds.plan_id is not None
    return funds.plan_id, {a.kind: a.logical_action_id for a in funds.actions}


def compose_body(browser: Browser, with_bounds: bool = True) -> tuple[str, dict]:
    source, funds = fund_drafts(browser, with_bounds)
    reserve = draft(browser, "reserve", {"target_fen": 500_000})
    rent = draft(browser, "pay-bill", {"commitment_id": "cmt_rent"})
    insurance = draft(browser, "pay-bill", {"commitment_id": "cmt_insurance"})
    ids = [
        reserve.logical_action_id,
        funds[ActionKind.REDEEM],
        funds[ActionKind.PURCHASE],
        funds[ActionKind.CHECK_OUTCOME],
        rent.logical_action_id,
        insurance.logical_action_id,
    ]
    depends_on = {funds[ActionKind.PURCHASE]: [funds[ActionKind.REDEEM]]}
    return source, {"draft_ids": ids, "depends_on": depends_on}


def composed(browser: Browser, with_bounds: bool = True) -> PlanView:
    _, body = compose_body(browser, with_bounds)
    result = ok(browser.post("/v1/plans/compose", body), PlanDraftResult, (201,))
    assert result.plan_id is not None
    return plan_view(browser, result.plan_id)


def card(browser: Browser, card_id: str) -> Card:
    return ok(browser.get(f"/v1/cards/{card_id}"), Card, (200,))


def confirm(browser: Browser, card_id: str):
    shown = card(browser, card_id)
    return browser.post(f"/v1/cards/{card_id}/confirm", {"terms_hash": shown.terms_hash})


def approved(browser: Browser, with_bounds: bool = True) -> PlanView:
    view = composed(browser, with_bounds)
    assert view.card_id is not None
    ok(confirm(browser, view.card_id), ConfirmResult, (201,))
    return plan_view(browser, view.plan_id)


def challenge(browser: Browser, logical_action_id: str) -> tuple[str, str]:
    issued = browser.post(f"/v1/actions/{logical_action_id}/challenge")
    assert issued.status_code == 201, issued.text
    challenge_id = issued.json()["challenge_id"]
    sms = SmsView.model_validate(browser.get("/v1/sms").json())
    return challenge_id, next(m.code for m in sms.messages if m.challenge_id == challenge_id)


def verify(browser: Browser, challenge_id: str, code: str):
    return browser.post(f"/v1/challenges/{challenge_id}/verify", {"code": code})


def pay(browser: Browser, logical_action_id: str) -> VerifyResult:
    return ok(verify(browser, *challenge(browser, logical_action_id)), VerifyResult, (200, 202))


def settled_at_t3(browser: Browser, with_bounds: bool = True) -> PlanView:
    """Plan approved, redemption sent at T+0, rent paid at T+2, redemption settled at T+3."""
    view = approved(browser, with_bounds)
    pay(browser, node(view, ActionKind.REDEEM).logical_action_id)
    advance(browser, at(2))
    pay(browser, node(view, ActionKind.PAY_BILL, "cmt_rent").logical_action_id)
    advance(browser, at(3))
    view = plan_view(browser, view.plan_id)
    assert node(view, ActionKind.PURCHASE).status is ActionStatus.AWAITING_STEPUP
    return view


def new_bill(browser: Browser, amount_fen: int, due: date = date(2026, 11, 11)) -> str:
    """A trusted e-bill confirmed by the customer through its own yellow card."""
    body = {
        "customer_id": "cus_main",
        "payee_id": "pay_wangfang",
        "kind": "培训费",
        "amount_fen": amount_fen,
        "due_date": due.isoformat(),
    }
    candidate = browser.sim("/sim/bills/ebill", body)
    assert candidate.status_code == 200, candidate.text
    commitment_id = candidate.json()["commitment_id"]
    confirm_bill = draft(browser, "bill", {"commitment_id": commitment_id})
    opened = browser.post(f"/v1/actions/{confirm_bill.logical_action_id}/request-approval")
    assert opened.status_code == 200, opened.text
    ok(confirm(browser, opened.json()["card_id"]), ConfirmResult, (201,))
    return commitment_id


def test_compose_joins_the_six_drafts_into_one_mixed_plan(browser: Browser):
    source, body = compose_body(browser)
    result = ok(browser.post("/v1/plans/compose", body), PlanDraftResult, (201,))
    assert result.kind is PlanKind.MIXED and result.validation.ok and len(result.actions) == 6
    view = plan_view(browser, result.plan_id)
    assert view.status is PlanStatus.DRAFT and view.root_approval_id is None
    assert view.bounds is not None and view.bounds.model_dump() == BOUNDS
    assert view.expires_at == at(9, 0)
    purchase = node(view, ActionKind.PURCHASE)
    assert purchase.depends_on == (node(view, ActionKind.REDEEM).logical_action_id,)
    assert node(view, ActionKind.PAY_BILL, "cmt_rent").not_before == at(2)
    assert node(view, ActionKind.PAY_BILL, "cmt_insurance").not_before == at(7)
    assert node(view, ActionKind.CHECK_OUTCOME).not_before == at(5)

    shown = card(browser, view.card_id)
    assert shown.kind is CardKind.PLAN and shown.status is CardStatus.OPEN
    assert shown.cash_snapshot_id is not None and shown.calc_result_id is not None
    keys = [line.key for line in shown.lines]
    assert keys[:3] == ["reserve", "redeem", "purchase"]
    assert "bill_cmt_rent" in keys and "bounds" in keys and "expires" in keys

    # The drafted rebalance plan is closed: not listed, its card can no longer be confirmed.
    listed = ok(browser.get("/v1/plans"), PlansView, (200,))
    assert [p.plan_id for p in listed.plans] == [view.plan_id]
    old = plan_view(browser, source)
    assert old.status is PlanStatus.PARTIAL and old.nodes == ()
    assert card(browser, old.card_id).status is CardStatus.EXPIRED
    refused(confirm(browser, old.card_id), ErrorCode.APPROVAL_EXPIRED)


def test_a_rejected_plan_creates_nothing_and_lists_every_code(browser: Browser):
    _, body = compose_body(browser)
    body["depends_on"] = {}
    plans_before = rows(browser, "SELECT COUNT(*) AS n FROM plans")
    response = browser.post("/v1/plans/compose", body)
    assert response.status_code == 422
    error = refused(response, ErrorCode.UNFUNDED_DEPENDENCY)
    assert error.detail["codes"] == ["UNFUNDED_DEPENDENCY", "PLAN_UNFUNDED"]
    assert rows(browser, "SELECT COUNT(*) AS n FROM plans") == plans_before
    assert rows(browser, "SELECT event FROM audit_log WHERE event = 'plan_compose_refused'")


def test_compose_refuses_another_customers_draft_as_not_found(browser: Browser):
    _, body = compose_body(browser)
    browser.login("cus_other")
    response = browser.post("/v1/plans/compose", body)
    assert response.status_code == 404


def test_plan_approval_raises_the_reserve_in_the_same_confirmation(browser: Browser):
    view = approved(browser)
    assert view.status is PlanStatus.ACTIVE
    assert node(view, ActionKind.DESIGNATE_RESERVE).status is ActionStatus.SETTLED
    assert node(view, ActionKind.REDEEM).status is ActionStatus.AWAITING_STEPUP
    assert rows(browser, "SELECT target_fen FROM reserves") == [{"target_fen": 500_000}]
    assert rows(browser, "SELECT COUNT(*) AS n FROM challenges") == [{"n": 0}]


def test_a_bill_within_bounds_revises_the_purchase_in_one_unit(browser: Browser):
    view = settled_at_t3(browser)
    purchase_id = node(view, ActionKind.PURCHASE).logical_action_id
    old_challenge, old_code = challenge(browser, purchase_id)

    new_bill(browser, 120_000)

    view = plan_view(browser, view.plan_id)
    revised = node(view, ActionKind.PURCHASE)
    assert (revised.revision, revised.amount_fen) == (2, 478_500)
    assert revised.status is ActionStatus.AWAITING_STEPUP
    stored = rows(
        browser,
        "SELECT revision, status, superseded_by, params_json FROM actions"
        " WHERE logical_action_id = ? ORDER BY revision",
        purchase_id,
    )
    assert [(r["revision"], r["status"], r["superseded_by"]) for r in stored] == [
        (1, "SUPERSEDED", 2),
        (2, "AWAITING_STEPUP", None),
    ]
    first, second = (json.loads(r["params_json"]) for r in stored)
    assert {k: v for k, v in first.items() if k != "amount_fen"} == {
        k: v for k, v in second.items() if k != "amount_fen"
    }
    closed = rows(
        browser,
        "SELECT invalidation_reason FROM challenges WHERE challenge_id = ?",
        old_challenge,
    )
    assert closed == [{"invalidation_reason": "new_revision"}]
    revision = rows(browser, "SELECT * FROM plan_revisions WHERE revision = 2")
    assert len(revision) == 1 and revision[0]["reason"] == "bill_change"
    assert revision[0]["within_bounds"] == 1
    assert revision[0]["approval_id"] == view.root_approval_id
    assert view.active_revision == 2
    assert rows(
        browser, "SELECT COUNT(*) AS n FROM domain_events WHERE type = ?", "plan_revision_created"
    ) == [{"n": 1}]
    assert rows(browser, "SELECT COUNT(*) AS n FROM approvals") == [{"n": 2}]

    refused(verify(browser, old_challenge, old_code), ErrorCode.SUPERSEDED)
    bought = pay(browser, purchase_id).operation
    assert bought is not None and bought.amount_fen == 478_500 and bought.revision == 2


def test_without_bounds_the_same_bill_pauses_the_exact_purchase(browser: Browser):
    view = settled_at_t3(browser, with_bounds=False)
    assert view.bounds is None
    new_bill(browser, 120_000)
    view = plan_view(browser, view.plan_id)
    purchase = node(view, ActionKind.PURCHASE)
    assert purchase.status is ActionStatus.PAUSED
    assert (purchase.revision, purchase.amount_fen) == (1, 498_500)
    assert set(purchase.pause_reasons) == {ErrorCode.EXACT_ORDERS_ONLY}
    assert rows(browser, "SELECT COUNT(*) AS n FROM plan_revisions WHERE revision = 2") == [
        {"n": 0}
    ]


def test_bills_count_from_the_original_approval_and_once_each(browser: Browser):
    view = settled_at_t3(browser)
    purchase_id = node(view, ActionKind.PURCHASE).logical_action_id

    new_bill(browser, 80_000, date(2026, 11, 11))
    assert node(plan_view(browser, view.plan_id), ActionKind.PURCHASE).revision == 1
    new_bill(browser, 40_000, date(2026, 11, 11))
    assert node(plan_view(browser, view.plan_id), ActionKind.PURCHASE).amount_fen == 478_500
    new_bill(browser, 40_000, date(2026, 11, 12))
    paused = node(plan_view(browser, view.plan_id), ActionKind.PURCHASE)
    assert paused.status is ActionStatus.PAUSED and paused.revision == 2
    assert set(paused.pause_reasons) == {
        ErrorCode.BILL_INCREASE_OVER_LIMIT,
        ErrorCode.RETENTION_OVER_LIMIT,
        ErrorCode.PURCHASE_BELOW_MIN,
    }

    before = rows(browser, "SELECT * FROM actions WHERE logical_action_id = ?", purchase_id)
    with browser.db.unit_of_work("rewind") as uow:
        uow.execute("DELETE FROM event_cursors")
    advance(browser, at(3, 10))
    assert rows(browser, "SELECT * FROM actions WHERE logical_action_id = ?", purchase_id) == before
    assert [r["revision"] for r in rows(browser, "SELECT revision FROM plan_revisions")] == [1, 2]


def test_a_bill_beyond_the_bounds_pauses_only_the_purchase(browser: Browser):
    view = settled_at_t3(browser)
    new_bill(browser, 160_000)
    view = plan_view(browser, view.plan_id)
    assert node(view, ActionKind.PURCHASE).status is ActionStatus.PAUSED
    assert view.status is PlanStatus.ACTIVE
    assert rows(browser, "SELECT target_fen FROM reserves") == [{"target_fen": 500_000}]

    advance(browser, at(5))
    view = plan_view(browser, view.plan_id)
    assert view.checkpoint is not None
    assert view.checkpoint.result is CheckpointResult.CHECKPOINT_MISSED
    advance(browser, at(7))
    pay(browser, node(view, ActionKind.PAY_BILL, "cmt_insurance").logical_action_id)
    assert plan_view(browser, view.plan_id).status is PlanStatus.PAUSED


def test_a_purchase_decided_before_a_new_bill_is_stale(browser: Browser, monkeypatch):
    view = settled_at_t3(browser)
    purchase_id = node(view, ActionKind.PURCHASE).logical_action_id
    challenge_id, code = challenge(browser, purchase_id)
    coordinator = browser.client.app.state.coordinator
    # The bill commits before the plan has decided again, as in a race with the code.
    monkeypatch.setattr(coordinator, "continue_plans", lambda customer_id: None)
    new_bill(browser, 120_000)

    refused(verify(browser, challenge_id, code), ErrorCode.STALE_SNAPSHOT)
    assert rows(browser, "SELECT COUNT(*) AS n FROM operations WHERE kind = 'PURCHASE'") == [
        {"n": 0}
    ]
    monkeypatch.undo()
    advance(browser, at(3, 10))
    revised = node(plan_view(browser, view.plan_id), ActionKind.PURCHASE)
    assert (revised.revision, revised.amount_fen) == (2, 478_500)
    assert revised.status is ActionStatus.AWAITING_STEPUP


def test_after_the_plan_expiry_codes_and_challenges_are_plan_approval_expired(
    browser: Browser,
):
    view = approved(browser)
    insurance = node(view, ActionKind.PAY_BILL, "cmt_insurance").logical_action_id
    advance(browser, at(8, 23, 59))
    challenge_id, code = challenge(browser, insurance)
    advance(browser, at(9, 0))

    refused(verify(browser, challenge_id, code), ErrorCode.PLAN_APPROVAL_EXPIRED)
    refused(browser.post(f"/v1/actions/{insurance}/challenge"), ErrorCode.PLAN_APPROVAL_EXPIRED)
    view = plan_view(browser, view.plan_id)
    assert view.status is PlanStatus.PARTIAL
    assert node(view, ActionKind.PAY_BILL, "cmt_insurance").status is ActionStatus.CANCELLED
    assert rows(
        browser, "SELECT COUNT(*) AS n FROM operations WHERE commitment_id = ?", "cmt_insurance"
    ) == [{"n": 0}]


@pytest.mark.case("T47")
def test_a_paused_purchase_states_the_amount_a_new_approval_would_buy(browser: Browser):
    view = settled_at_t3(browser)
    new_bill(browser, 160_000)
    state = browser.client.app.state
    paused = state.plans.plan("cus_main", view.plan_id)
    with browser.db.read() as conn:
        block = collect_facts(conn, [paused], EPOCH)
    feasible = [
        f.subject
        for f in block.facts
        if f.concept is FactConcept.OPTION_FEASIBILITY and f.value == FEASIBLE_WITH_APPROVAL
    ]
    amounts = [
        f.value
        for f in block.facts
        if f.concept is FactConcept.PURCHASE_AMOUNT and f.subject in feasible
    ]
    assert amounts == [438_500]
    assert not check_claims("您可自由支配4,385元，但买入4,385元是不可能的。", block).passed
    assert check_claims("买入已暂停，您目前可自由支配4,385元。", block).passed
    assert node(plan_view(browser, view.plan_id), ActionKind.PURCHASE).status is ActionStatus.PAUSED
    assert rows(browser, "SELECT COUNT(*) AS n FROM plan_revisions") == [{"n": 1}]
