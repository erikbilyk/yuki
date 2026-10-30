"""Flat mode (SPEC §26): the same agent and gateway without the plan coordinator."""

from datetime import datetime, timedelta

import pytest

from wg.agent.registry import Services, ToolContext, ToolRegistry
from wg.contracts.api import CandidatesView, Card, ConfirmResult, PlanView, SmsView
from wg.contracts.enums import ActionKind, ActionStatus, Channel, PlanMode, PlanStatus
from wg.contracts.errors import ErrorCode
from wg.k6.simtime import SHANGHAI

from .conftest import Browser

pytestmark = pytest.mark.layer("U")

EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)
SEARCH = {"sector": "technology", "target_max_pct": "40.0000", "fee_cap_fen": 2_000}


def registry(browser: Browser) -> ToolRegistry:
    state = browser.client.app.state
    return ToolRegistry(state.db, Services.from_state(state))


def context(mode: PlanMode = PlanMode.FLAT) -> ToolContext:
    return ToolContext(customer_id="cus_main", channel=Channel.WEB, mode=mode)


def rows(browser: Browser, sql: str, *params):
    with browser.db.read() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def flat_plan(browser: Browser) -> tuple[str, dict[ActionKind, str]]:
    found = browser.post("/v1/funds/candidates", SEARCH)
    assert found.status_code == 200, found.text
    calc_result_id = CandidatesView.model_validate(found.json()).calc_result_id
    drafted = registry(browser).call(
        context(),
        "draft_rebalance_plan",
        {"intent": "funds.rebalance", "calc_result_id": calc_result_id, "with_bounds": True},
    )
    assert drafted.ok, drafted.error
    result = drafted.result
    return result.plan_id, {a.kind: a.logical_action_id for a in result.actions}


def plan_view(browser: Browser, plan_id: str) -> PlanView:
    response = browser.get(f"/v1/plans/{plan_id}")
    assert response.status_code == 200, response.text
    return PlanView.model_validate(response.json())


def approve(browser: Browser, logical_action_id: str):
    """The model shows the order's own card; the customer confirms it and enters the code."""
    shown = registry(browser).call(
        context(),
        "request_approval",
        {"intent": "funds.rebalance", "logical_action_id": logical_action_id},
    )
    if not shown.ok:
        return shown.error
    card = Card.model_validate(browser.get(f"/v1/cards/{shown.result.card_id}").json())
    confirmed = browser.post(f"/v1/cards/{card.card_id}/confirm", {"terms_hash": card.terms_hash})
    result = ConfirmResult.model_validate(confirmed.json())
    assert result.challenge is not None
    sms = SmsView.model_validate(browser.get("/v1/sms").json())
    code = next(m.code for m in sms.messages if m.challenge_id == result.challenge.challenge_id)
    verified = browser.post(
        f"/v1/challenges/{result.challenge.challenge_id}/verify", {"code": code}
    )
    assert verified.status_code in (200, 202), verified.text
    return verified.json()


def advance_to_day(browser: Browser, day: int) -> None:
    target = (EPOCH + timedelta(days=day)).isoformat()
    assert browser.sim("/sim/clock/advance", {"to": target}).status_code == 200


def test_the_agent_mode_route_sets_current_customer_sessions(browser: Browser):
    assert rows(browser, "SELECT agent_mode FROM sessions WHERE role = 'customer'") == [
        {"agent_mode": "coordinated"}
    ]
    response = browser.sim("/sim/agent-mode", {"mode": "flat"})
    assert response.status_code == 204 and response.content == b""
    assert rows(browser, "SELECT agent_mode FROM sessions WHERE role = 'customer'") == [
        {"agent_mode": "flat"}
    ]
    assert rows(browser, "SELECT agent_mode FROM sessions WHERE role = 'demo'") == [
        {"agent_mode": "coordinated"}
    ]
    refused = browser.post("/sim/agent-mode", {"mode": "coordinated"})
    assert refused.status_code in (401, 403)
    assert rows(browser, "SELECT agent_mode FROM sessions WHERE role = 'customer'") == [
        {"agent_mode": "flat"}
    ]
    assert browser.sim("/sim/agent-mode", {"mode": "loose"}).status_code == 422


def test_a_flat_rebalance_is_two_orders_without_plan_card_or_dependency(browser: Browser):
    plan_id, nodes = flat_plan(browser)
    assert set(nodes) == {ActionKind.REDEEM, ActionKind.PURCHASE}
    [plan] = rows(
        browser, "SELECT kind, mode, status, root_approval_id FROM plans WHERE plan_id = ?", plan_id
    )
    assert plan == {
        "kind": "rebalance",
        "mode": "flat",
        "status": "draft",
        "root_approval_id": None,
    }
    assert rows(browser, "SELECT depends_on FROM actions WHERE plan_id = ?", plan_id) == [
        {"depends_on": "[]"},
        {"depends_on": "[]"},
    ]
    assert rows(browser, "SELECT card_id FROM cards") == []
    assert rows(browser, "SELECT approval_id FROM approvals") == []


def test_compose_is_refused_in_flat_mode_and_allowed_when_coordinated(browser: Browser):
    _, nodes = flat_plan(browser)
    arguments = {"intent": "plan.manage", "draft_ids": list(nodes.values()), "depends_on": {}}
    flat = registry(browser).call(context(), "compose_plan", arguments)
    assert flat.error is not None and flat.error.code is ErrorCode.UNSUPPORTED_REQUEST
    assert flat.error.detail == {"reason": "flat_mode"}
    refusals = rows(browser, "SELECT decision FROM audit_log WHERE event = 'tool_refused'")
    assert refusals == [{"decision": "UNSUPPORTED_REQUEST"}]
    # A coordinated session may not take a flat plan's orders into a plan of its own either.
    coordinated = registry(browser).call(context(PlanMode.COORDINATED), "compose_plan", arguments)
    assert coordinated.error is not None
    assert coordinated.error.detail.get("reason") == "in_another_plan"
    assert rows(browser, "SELECT COUNT(*) AS n FROM plans WHERE kind = 'mixed'") == [{"n": 0}]


def test_each_flat_order_is_approved_alone_and_the_gateway_still_guards_proceeds(
    browser: Browser,
):
    plan_id, nodes = flat_plan(browser)
    early = approve(browser, nodes[ActionKind.PURCHASE])
    assert ErrorCode.UNSETTLED_FUNDS.value in early.detail["reasons"]
    redeemed = approve(browser, nodes[ActionKind.REDEEM])
    assert redeemed["operation"]["status"] == "SUBMITTED"
    assert plan_view(browser, plan_id).status is PlanStatus.ACTIVE

    advance_to_day(browser, 3)
    bought = approve(browser, nodes[ActionKind.PURCHASE])
    assert bought["operation"]["status"] == "SETTLED"
    advance_to_day(browser, 5)

    view = plan_view(browser, plan_id)
    assert view.status is PlanStatus.COMPLETED and view.checkpoint is None
    assert {n.status for n in view.nodes} == {ActionStatus.SETTLED}
    assert rows(browser, "SELECT COUNT(*) AS n FROM plan_revisions") == [{"n": 0}]
    assert rows(browser, "SELECT COUNT(*) AS n FROM checkpoint_results") == [{"n": 0}]
    scopes = rows(browser, "SELECT DISTINCT scope_kind FROM approvals WHERE plan_id = ?", plan_id)
    assert scopes == [{"scope_kind": "action"}]


def test_the_tick_and_continuation_leave_a_flat_order_alone(browser: Browser):
    """A bill confirmed after the purchase's code was asked for revises nothing in flat mode."""
    plan_id, nodes = flat_plan(browser)
    approve(browser, nodes[ActionKind.REDEEM])
    advance_to_day(browser, 3)
    shown = registry(browser).call(
        context(),
        "request_approval",
        {"intent": "funds.rebalance", "logical_action_id": nodes[ActionKind.PURCHASE]},
    )
    card = Card.model_validate(browser.get(f"/v1/cards/{shown.result.card_id}").json())
    browser.post(f"/v1/cards/{card.card_id}/confirm", {"terms_hash": card.terms_hash})

    candidate = browser.sim(
        "/sim/bills/ebill",
        {
            "customer_id": "cus_main",
            "payee_id": "pay_wangfang",
            "kind": "培训费",
            "amount_fen": 120_000,
            "due_date": "2026-11-11",
        },
    )
    bill = browser.post("/v1/drafts/bill", {"commitment_id": candidate.json()["commitment_id"]})
    opened = browser.post(f"/v1/actions/{bill.json()['logical_action_id']}/request-approval")
    bill_card = Card.model_validate(browser.get(f"/v1/cards/{opened.json()['card_id']}").json())
    browser.post(f"/v1/cards/{bill_card.card_id}/confirm", {"terms_hash": bill_card.terms_hash})
    advance_to_day(browser, 4)

    purchase = rows(
        browser,
        "SELECT revision, status FROM actions WHERE logical_action_id = ?",
        nodes[ActionKind.PURCHASE],
    )
    assert purchase == [{"revision": 1, "status": "AWAITING_STEPUP"}]
    assert rows(browser, "SELECT COUNT(*) AS n FROM plan_revisions") == [{"n": 0}]
    assert plan_view(browser, plan_id).status is PlanStatus.ACTIVE
