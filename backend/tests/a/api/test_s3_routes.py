"""S3 over HTTP in process: candidates, plan draft and card, approval, fund orders, checkpoint."""

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from wg.contracts.api import (
    CandidatesView,
    Card,
    CardKind,
    ConfirmResult,
    ExposureView,
    FeeEstimateView,
    PlanDraftResult,
    PlansView,
    PlanView,
    PositionsView,
    ReceiptView,
    SmsView,
    StressView,
    VerifyResult,
)
from wg.contracts.enums import (
    ActionKind,
    ActionStatus,
    CheckpointResult,
    OperationStatus,
    PlanStatus,
    Tier,
)
from wg.contracts.errors import ErrorBody, ErrorCode
from wg.k6.simtime import SHANGHAI

from .conftest import Browser

pytestmark = pytest.mark.layer("H")

EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)
FUND_C, FUND_D = "990003", "990004"
SEARCH = {"sector": "technology", "target_max_pct": "40.0000", "fee_cap_fen": 2_000}


def ok(response, model, status=(200, 201)):
    assert response.status_code in status, response.text
    return model.model_validate(response.json())


def error(response) -> ErrorBody:
    return ErrorBody.model_validate(response.json())


def search(browser: Browser, **body) -> CandidatesView:
    return ok(browser.post("/v1/funds/candidates", {**SEARCH, **body}), CandidatesView, (200,))


def plan_view(browser: Browser, plan_id: str) -> PlanView:
    return ok(browser.get(f"/v1/plans/{plan_id}"), PlanView, (200,))


def node(view: PlanView, kind: ActionKind):
    return next(n for n in view.nodes if n.kind is kind)


def drafted(browser: Browser, with_bounds: bool = False) -> PlanView:
    found = search(browser)
    body = {"calc_result_id": found.calc_result_id, "with_bounds": with_bounds}
    draft = ok(browser.post("/v1/funds/plans", body), PlanDraftResult, (201,))
    assert draft.plan_id is not None
    return plan_view(browser, draft.plan_id)


def card(browser: Browser, card_id: str) -> Card:
    return ok(browser.get(f"/v1/cards/{card_id}"), Card, (200,))


def approved(browser: Browser, with_bounds: bool = False) -> PlanView:
    view = drafted(browser, with_bounds)
    assert view.card_id is not None
    shown = card(browser, view.card_id)
    confirm = browser.post(f"/v1/cards/{shown.card_id}/confirm", {"terms_hash": shown.terms_hash})
    result = ok(confirm, ConfirmResult, (201,))
    assert result.plan_id == view.plan_id and result.operation is None
    return plan_view(browser, view.plan_id)


def code_for(browser: Browser, logical_action_id: str) -> VerifyResult:
    challenge = browser.post(f"/v1/actions/{logical_action_id}/challenge")
    assert challenge.status_code == 201, challenge.text
    challenge_id = challenge.json()["challenge_id"]
    sms = SmsView.model_validate(browser.get("/v1/sms").json())
    code = next(m.code for m in sms.messages if m.challenge_id == challenge_id)
    verified = browser.post(f"/v1/challenges/{challenge_id}/verify", {"code": code})
    return ok(verified, VerifyResult, (200, 202))


def advance_to_day(browser: Browser, day: int) -> None:
    target = (EPOCH + timedelta(days=day)).isoformat()
    assert browser.sim("/sim/clock/advance", {"to": target}).status_code == 200


def rows(browser: Browser, sql: str, *params):
    with browser.db.read() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def test_search_finds_the_one_passing_swap_and_stores_its_basis(browser: Browser):
    found = search(browser)
    chosen = found.result.selected
    assert chosen is not None and (chosen.sell_product, chosen.buy_product) == (FUND_C, FUND_D)
    assert (chosen.gross_fen, chosen.fee_fen, chosen.purchase_fen) == (500_000, 1_500, 498_500)
    assert chosen.checkpoint.upper_pct == Decimal("39.5089")
    assert [c.kind.value for c in found.result.candidates].count("swap") == 60

    stored = rows(
        browser, "SELECT * FROM calc_results WHERE calc_result_id = ?", found.calc_result_id
    )
    assert len(stored) == 1 and stored[0]["kind"] == "candidates"
    basis = stored[0]["basis_json"]
    assert found.result.cash_snapshot_id in basis and "synthetic_0.3pct_v1" in basis
    # The same search on the same rows is the same result.
    assert search(browser).calc_result_id == found.calc_result_id


def test_reads_positions_exposure_fees_and_stress(browser: Browser):
    positions = ok(browser.get("/v1/funds/positions"), PositionsView, (200,))
    assert positions.total_fen == 5_000_000
    exposure = ok(
        browser.get("/v1/funds/exposure", key="technology", target_max_pct="40.0000"),
        ExposureView,
        (200,),
    )
    assert exposure.exposure.upper_pct == Decimal("45.5000")
    fee = ok(
        browser.get("/v1/funds/fees/estimate", product_id=FUND_C, shares="5000.0000"),
        FeeEstimateView,
        (200,),
    )
    assert (fee.quote.fee_fen, fee.quote.net_fen) == (1_500, 498_500)
    found = search(browser)
    stressed = ok(
        browser.post("/v1/funds/stress", {"calc_result_id": found.calc_result_id}),
        StressView,
        (200,),
    )
    losses = {r.shock: (r.before_loss_fen, r.after_loss_fen) for r in stressed.result.rows}
    assert losses["market"] == (1_070_000, 1_036_230)
    assert losses["target_fund"] == (675_000, 850_900)


def test_plan_draft_shows_a_yellow_card_and_grants_nothing(browser: Browser):
    view = drafted(browser, with_bounds=True)
    assert view.status is PlanStatus.DRAFT and view.root_approval_id is None
    assert view.bounds is not None and view.bounds.p0_fen == 498_500
    redeem, purchase = node(view, ActionKind.REDEEM), node(view, ActionKind.PURCHASE)
    check = node(view, ActionKind.CHECK_OUTCOME)
    assert redeem.shares == Decimal("5000.0000") and redeem.tier is Tier.RED
    assert purchase.amount_fen == 498_500 and purchase.depends_on == (redeem.logical_action_id,)
    assert check.not_before == EPOCH + timedelta(days=5) and check.tier is Tier.GREEN

    assert view.card_id is not None
    shown = card(browser, view.card_id)
    assert shown.kind is CardKind.PLAN and shown.tier is Tier.YELLOW and not shown.requires_code
    keys = {line.key for line in shown.lines}
    assert {"redeem", "purchase", "fee", "turnover", "target", "checkpoint", "expires"} <= keys
    assert {"bounds", "stress_target_fund"} <= keys
    assert any("¥1,759.00" in warning for warning in shown.warnings)
    assert shown.expires_at == EPOCH.replace(hour=0) + timedelta(days=9)
    assert rows(browser, "SELECT COUNT(*) AS n FROM approvals") == [{"n": 0}]

    # A node of a plan is approved with its plan card, not on a card of its own.
    refused = browser.post(f"/v1/actions/{redeem.logical_action_id}/request-approval")
    assert refused.status_code == 422 and error(refused).code is ErrorCode.UNSUPPORTED_REQUEST


def test_plan_runs_from_approval_to_a_passed_checkpoint(browser: Browser):
    view = approved(browser)
    assert view.status is PlanStatus.ACTIVE and view.expires_at is not None
    assert [r.reason.value for r in view.revisions] == ["initial"]
    redeem = node(view, ActionKind.REDEEM)
    assert redeem.status is ActionStatus.AWAITING_STEPUP
    assert node(view, ActionKind.PURCHASE).status is ActionStatus.DRAFT

    submitted = code_for(browser, redeem.logical_action_id).operation
    assert submitted is not None and submitted.status is OperationStatus.SUBMITTED
    assert submitted.settle_due_at == EPOCH + timedelta(days=3)
    held = ok(browser.get("/v1/funds/positions"), PositionsView, (200,))
    fund_c = next(p for p in held.positions if p.product_id == FUND_C)
    assert (fund_c.shares, fund_c.free_shares) == (Decimal("10000.0000"), Decimal("5000.0000"))

    advance_to_day(browser, 3)
    receipt = ok(browser.get(f"/v1/receipts/{submitted.operation_id}"), ReceiptView, (200,))
    assert (receipt.amount_fen, receipt.fee_fen, receipt.net_fen) == (500_000, 1_500, 498_500)
    entries = rows(
        browser,
        "SELECT account_id, amount_fen FROM ledger_entries WHERE operation_id = ?"
        " ORDER BY account_id",
        submitted.operation_id,
    )
    assert entries == [
        {"account_id": "acc_main", "amount_fen": 498_500},
        {"account_id": "acc_sys_fee_income", "amount_fen": 1_500},
        {"account_id": "acc_sys_fund_settlement", "amount_fen": -500_000},
    ]
    assert rows(browser, "SELECT shares FROM lots WHERE product_id = ?", FUND_C) == [
        {"shares": "5000.0000"}
    ]

    purchase = node(plan_view(browser, view.plan_id), ActionKind.PURCHASE)
    assert purchase.status is ActionStatus.AWAITING_STEPUP
    bought = code_for(browser, purchase.logical_action_id).operation
    assert bought is not None and bought.status is OperationStatus.SETTLED
    assert bought.shares == Decimal("4985.0000")
    lots = rows(browser, "SELECT shares FROM lots WHERE product_id = ? ORDER BY shares", FUND_D)
    assert lots == [{"shares": "4985.0000"}, {"shares": "5000.0000"}]

    advance_to_day(browser, 5)
    done = plan_view(browser, view.plan_id)
    assert done.checkpoint is not None and done.checkpoint.result is CheckpointResult.PASSED
    assert done.checkpoint.measured_pct == Decimal("39.5089")
    assert done.status is PlanStatus.COMPLETED
    assert done.next_operation is None


def test_checkpoint_is_missed_when_the_purchase_never_ran(browser: Browser):
    view = approved(browser)
    code_for(browser, node(view, ActionKind.REDEEM).logical_action_id)
    advance_to_day(browser, 7)
    after = plan_view(browser, view.plan_id)
    assert after.checkpoint is not None
    assert after.checkpoint.result is CheckpointResult.CHECKPOINT_MISSED
    assert after.checkpoint.evaluated_at == EPOCH + timedelta(days=5)
    assert after.checkpoint.measured_pct == Decimal("42.7778")
    assert after.status is PlanStatus.ACTIVE


def test_search_without_a_passing_swap_names_the_closest_reason(browser: Browser):
    found = search(browser, fee_cap_fen=1_499)
    assert found.result.selected is None
    refused = browser.post("/v1/funds/plans", {"calc_result_id": found.calc_result_id})
    assert refused.status_code == 422
    body = error(refused)
    assert body.code is ErrorCode.FEE_CAP and body.detail["codes"] == ["FEE_CAP"]
    assert ok(browser.get("/v1/plans"), PlansView, (200,)).plans == ()
    assert rows(browser, "SELECT COUNT(*) AS n FROM cards") == [{"n": 0}]


def test_unknown_lots_refuse_the_plan_as_incomplete_data(browser: Browser):
    assert browser.sim("/sim/reset", {"variant": "t43"}).status_code == 200
    found = search(browser)
    refused = browser.post("/v1/funds/plans", {"calc_result_id": found.calc_result_id})
    assert refused.status_code == 409 and error(refused).code is ErrorCode.DATA_INCOMPLETE
    assert rows(browser, "SELECT COUNT(*) AS n FROM actions WHERE kind = 'REDEEM'") == [{"n": 0}]


def test_card_with_changed_nodes_is_refused(browser: Browser):
    view = drafted(browser)
    redeem = node(view, ActionKind.REDEEM)
    with browser.db.unit_of_work("test tamper") as uow:
        uow.execute(
            "UPDATE actions SET params_json = ? WHERE logical_action_id = ?",
            (
                '{"account_id":"acc_main","product_id":"990003","shares":"6000.0000"}',
                redeem.logical_action_id,
            ),
        )
    assert view.card_id is not None
    shown = card(browser, view.card_id)
    refused = browser.post(f"/v1/cards/{shown.card_id}/confirm", {"terms_hash": shown.terms_hash})
    assert refused.status_code == 409 and error(refused).code is ErrorCode.TERMS_MISMATCH
    assert plan_view(browser, view.plan_id).root_approval_id is None
    assert rows(browser, "SELECT COUNT(*) AS n FROM approvals") == [{"n": 0}]


def test_stop_cancels_unsubmitted_nodes_and_foreign_plans_are_not_found(browser: Browser):
    view = approved(browser)
    stopped = ok(browser.post(f"/v1/plans/{view.plan_id}/stop"), PlanView, (200,))
    assert stopped.status is PlanStatus.PARTIAL
    assert {n.status for n in stopped.nodes} == {ActionStatus.CANCELLED}
    assert ok(browser.post(f"/v1/plans/{view.plan_id}/stop"), PlanView, (200,)) == stopped

    browser.login("cus_other")
    foreign = browser.get(f"/v1/plans/{view.plan_id}")
    missing = browser.get("/v1/plans/pln_nothingthere0001")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    stop_foreign = browser.post(f"/v1/plans/{view.plan_id}/stop")
    assert stop_foreign.status_code == 404 and stop_foreign.json() == missing.json()


def _reject_float(text: str):
    raise AssertionError(f"float {text} in a response")


def _money_fields_are_integers(value, path="$") -> list[str]:
    problems = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_fen") and item is not None and type(item) is not int:
                problems.append(f"{path}.{key}")
            problems += _money_fields_are_integers(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            problems += _money_fields_are_integers(item, f"{path}[{index}]")
    return problems


def test_fund_and_plan_responses_carry_no_float(browser: Browser):
    view = approved(browser, with_bounds=True)
    found = search(browser)
    responses = [
        browser.get("/v1/funds/positions"),
        browser.get("/v1/funds/exposure", key="technology", target_max_pct="40.0000"),
        browser.get("/v1/funds/fees/estimate", product_id=FUND_C, shares="5000.0000"),
        browser.post("/v1/funds/candidates", SEARCH),
        browser.post("/v1/funds/stress", {"calc_result_id": found.calc_result_id}),
        browser.get("/v1/plans"),
        browser.get(f"/v1/plans/{view.plan_id}"),
        browser.get(f"/v1/cards/{view.card_id}"),
    ]
    for response in responses:
        assert response.status_code == 200, response.text
        body = json.loads(response.text, parse_float=_reject_float)
        assert _money_fields_are_integers(body) == [], response.request.url
