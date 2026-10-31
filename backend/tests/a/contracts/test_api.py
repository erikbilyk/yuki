from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from wg.contracts import api, tools
from wg.contracts.api import (
    NODE_PHRASE,
    ROUTES,
    BillDraftRequest,
    Card,
    CardKind,
    CardStatus,
    CashFields,
    CashMode,
    ClockAdvanceRequest,
    DraftResult,
    MoneyChange,
    PercentChange,
    PercentQuality,
    PlanDraftResult,
    PlanNodeView,
    ReceiptView,
    RouteGroup,
    RouteRole,
    find_route,
    route_models,
)
from wg.contracts.enums import ActionKind, ActionStatus, PlanKind, ReceiptStatus, Tier
from wg.contracts.errors import ErrorBody, ErrorCode
from wg.contracts.plan import PlanValidationResult

NOW = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
HASH = "a" * 64

# Every route of SPEC §19.2 (the draft routes without a method are POST), plus /healthz.
SPEC_ROUTES = """
POST /v1/auth/login
POST /v1/auth/logout
GET /v1/session
GET /v1/accounts
GET /v1/accounts/{id}/balance
GET /v1/payees
GET /v1/transfers/history
GET /v1/operations/{id}
GET /v1/receipts/{operation_id}
GET /v1/cash/snapshot
GET /v1/bills
GET /v1/spending
GET /v1/transactions
GET /v1/unresolved
GET /v1/funds/exposure
GET /v1/funds/positions
GET /v1/funds/public-diagnostics
POST /v1/funds/candidates
POST /v1/funds/stress
GET /v1/funds/fees/estimate
GET /v1/plans
GET /v1/plans/{id}
GET /v1/audit
POST /v1/drafts/transfer
POST /v1/drafts/bill
POST /v1/drafts/reserve
POST /v1/drafts/resolution
POST /v1/drafts/pay-bill
POST /v1/drafts/mark-paid-external
POST /v1/drafts/cancel-bill
POST /v1/funds/plans
POST /v1/plans/compose
POST /v1/actions/{id}/request-approval
POST /v1/actions/{id}/cancel
GET /v1/cards/{id}
POST /v1/cards/{id}/confirm
POST /v1/actions/{id}/challenge
POST /v1/challenges/{id}/verify
POST /v1/challenges/{id}/reissue
POST /v1/plans/{id}/stop
GET /v1/sms
POST /v1/channels/pairing-code
POST /v1/chat/messages
GET /v1/chat/history
GET /v1/operator/locks
POST /v1/operator/locks/{id}/unlock
POST /v1/operator/reconcile
GET /v1/operator/audit
POST /sim/reset
POST /sim/clock/advance
POST /sim/faults
POST /sim/import
POST /sim/bills/ebill
POST /sim/sms
POST /sim/incoming-payment
POST /sim/external-debit
POST /sim/crash
POST /sim/agent-mode
POST /v1/channels/feishu/messages
GET /healthz
"""

# The direct-debit routes of S5-SPEC §3.5 built so far; each arrives with its handler.
S5_ROUTES = """
GET /v1/mandates
GET /v1/merchants
GET /v1/debit-requests
GET /v1/debit-requests/{id}
GET /v1/subscriptions/spend
POST /sim/debit-requests
"""


def _spec_routes() -> set[tuple[str, str]]:
    lines = (SPEC_ROUTES + S5_ROUTES).strip().splitlines()
    return {tuple(line.split()) for line in lines if line}


def test_routes_equal_the_spec_table():
    table = [(route.method, route.path) for route in ROUTES]
    assert len(table) == len(set(table))
    assert set(table) == _spec_routes()


def test_csrf_is_required_on_every_state_changing_human_route():
    for route in ROUTES:
        human_post = route.method == "POST" and route.role not in (
            RouteRole.PUBLIC,
            RouteRole.CHANNEL,
        )
        assert route.csrf == human_post, route.path


def test_roles_follow_the_route_groups():
    for route in ROUTES:
        if route.path.startswith("/sim/"):
            assert (route.role, route.group, route.demo_only) == (
                RouteRole.DEMO,
                RouteGroup.SIMULATOR,
                True,
            )
        elif route.path.startswith("/v1/operator/"):
            assert (route.role, route.group) == (RouteRole.OPERATOR, RouteGroup.OPERATOR)
        else:
            assert not route.demo_only
        if route.group in (RouteGroup.READS, RouteGroup.DRAFTS, RouteGroup.TRUSTED):
            assert route.role is RouteRole.CUSTOMER, route.path
    assert find_route("POST", "/v1/channels/feishu/messages").role is RouteRole.CHANNEL
    assert find_route("POST", "/v1/auth/login").role is RouteRole.PUBLIC


def test_routes_carry_bodies_where_needed():
    for route in ROUTES:
        if route.method == "GET":
            assert route.request is None
            assert route.response is not None, route.path
        else:
            assert route.query is None
    assert find_route("POST", "/sim/crash").response is None
    assert find_route("POST", "/v1/cards/{id}/confirm").request is api.ConfirmRequest
    assert find_route("GET", "/v1/cards/{id}").response is Card
    with pytest.raises(KeyError):
        find_route("DELETE", "/v1/plans/{id}")


def _all_models() -> list[type[BaseModel]]:
    models = [*route_models(), ErrorBody]
    for tool in tools.TOOLS:
        models += [tool.arguments, tool.result]
    return list(dict.fromkeys(models))


def _walk(node, path=()):
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, (*path, index))


def _types(schema: dict) -> set[str]:
    found = set()
    if "type" in schema:
        found.add(schema["type"])
    for option in schema.get("anyOf", ()):
        found |= _types(option)
    return found


@pytest.mark.parametrize("mode", ["validation", "serialization"])
@pytest.mark.parametrize("model", _all_models(), ids=lambda model: model.__name__)
def test_schemas_generate_without_numbers(model, mode):
    schema = model.model_json_schema(mode=mode)
    for path, node in _walk(schema):
        assert node.get("type") != "number", (model.__name__, path)
        if path and path[-2:-1] == ("properties",):
            name = path[-1]
            if name.endswith("_fen"):
                assert _types(node) <= {"integer", "null"}, (model.__name__, name)
            if name.endswith("_pct") or name in ("shares", "free_shares", "nav"):
                assert _types(node) <= {"string", "null"}, (model.__name__, name)


def _no_floats(value, path=()):
    if isinstance(value, float):
        raise AssertionError(f"float at {path}")
    if isinstance(value, dict):
        for key, item in value.items():
            _no_floats(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _no_floats(item, (*path, index))


def _card(**changes) -> Card:
    fields = dict(
        card_id="crd_1",
        kind=CardKind.ACTION,
        status=CardStatus.OPEN,
        title="转账给妈妈",
        tier=Tier.YELLOW,
        requires_code=False,
        logical_action_id="act_1",
        revision=1,
        action_kind=ActionKind.TRANSFER,
        lines=[{"key": "amount", "label": "金额", "text": "¥500.00"}],
        changes=[
            {
                "unit": "fen",
                "key": "headroom",
                "label": "可自由支配",
                "before": 200000,
                "after": 150000,
            },
            {
                "unit": "percent",
                "key": "tech",
                "label": "科技占比",
                "before": "45.5",
                "after": "39.5089",
                "quality": "estimated",
            },
        ],
        sources=[{"slot": "amount", "provenance": "user_utterance", "label": "来自您的消息"}],
        terms_hash=HASH,
        expires_at=NOW,
    )
    fields.update(changes)
    return Card.model_validate(fields)


def test_card_serializes_money_as_int_and_percent_as_string():
    card = _card()
    assert isinstance(card.changes[0], MoneyChange)
    assert isinstance(card.changes[1], PercentChange)
    data = card.model_dump(mode="json")
    assert data["changes"][0]["after"] == 150000
    assert data["changes"][1]["before"] == "45.5000"
    assert data["changes"][1]["after"] == "39.5089"
    _no_floats(data)


def test_card_rejects_float_money_and_inconsistent_states():
    with pytest.raises(ValidationError):
        _card(changes=[{"unit": "fen", "key": "k", "label": "l", "after": 1.5}])
    with pytest.raises(ValidationError):
        _card(changes=[{"unit": "percent", "key": "k", "label": "l", "after": 39.5}])
    with pytest.raises(ValidationError):
        _card(tier=Tier.RED)  # requires_code stays False
    with pytest.raises(ValidationError):
        _card(tier=Tier.FORBIDDEN)
    with pytest.raises(ValidationError):
        _card(status=CardStatus.CONFIRMED)  # no approval_id
    with pytest.raises(ValidationError):
        _card(revision=None)
    with pytest.raises(ValidationError):
        _card(kind=CardKind.PLAN)  # no plan_id
    with pytest.raises(ValidationError):
        _card(kind=CardKind.PLAN, plan_id="pln_1", parent_approval_id="apr_0")
    plan_card = _card(kind=CardKind.PLAN_REVISION, plan_id="pln_1", parent_approval_id="apr_0")
    assert plan_card.parent_approval_id == "apr_0"
    _card(status=CardStatus.CONFIRMED, approval_id="apr_1")


def _fields(**changes) -> dict:
    fields = dict(
        snapshot_id="snp_1",
        as_of=NOW,
        mode=CashMode.HEADROOM,
        headroom_fen=100000,
        shortfall_fen=0,
        protected_fen=1700000,
        unpaid_bills_fen=840000,
        unpaid_bills_count=2,
    )
    fields.update(changes)
    return fields


def test_cash_fields_mode_follows_shortfall():
    CashFields.model_validate(_fields())
    CashFields.model_validate(_fields(mode="shortfall", headroom_fen=0, shortfall_fen=1500))
    with pytest.raises(ValidationError):
        CashFields.model_validate(_fields(shortfall_fen=1500))
    with pytest.raises(ValidationError):
        CashFields.model_validate(_fields(mode="shortfall"))
    with pytest.raises(ValidationError):
        CashFields.model_validate(_fields(headroom_fen=100000.0))


def test_cash_snapshot_view_round_trips_through_json():
    details = {
        "rules_version": "s2_rules_v1",
        "c_fen": 1800000,
        "e_fen": 500000,
        "l_fen": 300000,
        "b_fen": 840000,
        "m_fen": 60000,
        "u_fen": 0,
        "accounts": [
            {
                "account_id": "acc_main",
                "name": "主账户",
                "covered": True,
                "posted_fen": 1800000,
                "available_fen": 1800000,
                "reconciled": True,
            }
        ],
        "monthly_estimate_fen": 1140000,
        "bills": [
            {
                "commitment_id": "cmt_rent",
                "unpaid_fen": 340000,
                "due_date": "2026-11-04",
                "status": "secured",
            }
        ],
        "quality": {
            "as_of": NOW.isoformat(),
            "uncovered_accounts": [],
            "unresolved_count": 0,
            "unresolved_impact_fen": 0,
            "reconciled": {"acc_main": True},
            "assumptions": [],
            "incomplete": False,
        },
    }
    view = api.CashSnapshotView.model_validate(_fields(details=details))
    data = view.model_dump(mode="json")
    _no_floats(data)
    assert data["details"]["c_fen"] == 1800000
    assert api.CashSnapshotView.model_validate_json(view.model_dump_json()) == view


def test_draft_result_feasible_exactly_without_reasons():
    base = dict(
        logical_action_id="act_1",
        revision=1,
        kind=ActionKind.TRANSFER,
        status=ActionStatus.DRAFT,
        tier=Tier.YELLOW,
    )
    DraftResult(**base, feasible=True)
    DraftResult(**base, feasible=False, reasons=[ErrorCode.INSUFFICIENT_FUNDS])
    with pytest.raises(ValidationError):
        DraftResult(**base, feasible=False)
    with pytest.raises(ValidationError):
        DraftResult(**base, feasible=True, reasons=[ErrorCode.INSUFFICIENT_FUNDS])


def test_plan_draft_has_a_plan_exactly_when_valid():
    ok = PlanValidationResult(ok=True)
    bad = PlanValidationResult(ok=False, codes=[ErrorCode.UNFUNDED_DEPENDENCY])
    PlanDraftResult(plan_id="pln_1", kind=PlanKind.MIXED, validation=ok)
    PlanDraftResult(kind=PlanKind.MIXED, validation=bad)
    with pytest.raises(ValidationError):
        PlanDraftResult(kind=PlanKind.MIXED, validation=ok)
    with pytest.raises(ValidationError):
        PlanDraftResult(plan_id="pln_1", kind=PlanKind.MIXED, validation=bad)


def test_node_phrase_covers_every_action_status():
    assert set(NODE_PHRASE) == set(ActionStatus)
    assert NODE_PHRASE[ActionStatus.AWAITING_STEPUP] is api.NodePhrase.AWAITING_CODE
    assert NODE_PHRASE[ActionStatus.PAUSED] is api.NodePhrase.NEEDS_DECISION


def _node(**changes) -> PlanNodeView:
    fields = dict(
        logical_action_id="act_buy",
        revision=2,
        kind=ActionKind.PURCHASE,
        status=ActionStatus.SETTLED,
        phrase=api.NodePhrase.DONE,
        tier=Tier.RED,
        amount_fen=478500,
        product_id="990004",
        receipt_id="rcp_1",
    )
    fields.update(changes)
    return PlanNodeView.model_validate(fields)


def test_plan_node_pause_reasons_only_when_paused():
    assert _node().model_dump(mode="json")["amount_fen"] == 478500
    _node(
        status=ActionStatus.PAUSED,
        phrase=api.NodePhrase.NEEDS_DECISION,
        pause_reasons=["BILL_INCREASE_OVER_LIMIT", "RETENTION_OVER_LIMIT", "PURCHASE_BELOW_MIN"],
        receipt_id=None,
    )
    with pytest.raises(ValidationError):
        _node(status=ActionStatus.PAUSED)
    with pytest.raises(ValidationError):
        _node(pause_reasons=["DATA_INCOMPLETE"])


def test_checkpoint_keeps_four_place_percent_strings():
    view = api.CheckpointView(
        logical_action_id="act_check",
        result="checkpoint_missed",
        measured_pct="42.7778",
        lower_pct="42.7778",
        upper_pct="42.7778",
        target_max_pct="40",
        quality=PercentQuality.CONFIRMED,
        evaluated_at=NOW,
    )
    data = view.model_dump(mode="json")
    assert data["measured_pct"] == "42.7778"
    assert data["target_max_pct"] == "40.0000"
    with pytest.raises(ValidationError):
        api.CheckpointView.model_validate({**data, "measured_pct": 42.7778})


def test_receipt_reason_only_when_rejected():
    base = dict(
        receipt_id="rcp_1",
        operation_id="opn_1",
        logical_action_id="act_1",
        revision=1,
        kind=ActionKind.REDEEM,
        account_id="acc_main",
        amount_fen=500000,
        fee_fen=1500,
        net_fen=498500,
        shares=Decimal("5000"),
        product_id="990003",
        created_at=NOW,
    )
    receipt = ReceiptView(**base, status=ReceiptStatus.SETTLED)
    assert receipt.model_dump(mode="json")["shares"] == "5000.0000"
    ReceiptView(**base, status=ReceiptStatus.REJECTED, reason_code=ErrorCode.INSUFFICIENT_FUNDS)
    with pytest.raises(ValidationError):
        ReceiptView(**base, status=ReceiptStatus.REJECTED)
    with pytest.raises(ValidationError):
        ReceiptView(**base, status=ReceiptStatus.SETTLED, reason_code=ErrorCode.INSUFFICIENT_FUNDS)


def test_clock_advance_takes_one_form():
    ClockAdvanceRequest(days=3)
    ClockAdvanceRequest(days=0, minutes=6)
    ClockAdvanceRequest(to=NOW)
    with pytest.raises(ValidationError):
        ClockAdvanceRequest()
    with pytest.raises(ValidationError):
        ClockAdvanceRequest(to=NOW, days=1)
    with pytest.raises(ValidationError):
        ClockAdvanceRequest(days=-1)


def test_bill_draft_takes_one_form():
    BillDraftRequest(commitment_id="cmt_new")
    BillDraftRequest(
        purpose="幼儿园学费", payee_id="pay_wangfang", amount_fen=120000, due_date=date(2026, 11, 9)
    )
    with pytest.raises(ValidationError):
        BillDraftRequest(commitment_id="cmt_new", amount_fen=120000)
    with pytest.raises(ValidationError):
        BillDraftRequest(purpose="幼儿园学费", amount_fen=120000)


def test_request_bodies_refuse_unknown_fields_and_float_money():
    with pytest.raises(ValidationError):
        api.ConfirmRequest(terms_hash=HASH, user_confirmed=True)
    with pytest.raises(ValidationError):
        api.TransferDraftRequest.model_validate({"payee_id": "pay_mom", "amount_fen": 500.0})
    with pytest.raises(ValidationError):
        api.TransferDraftRequest.model_validate({"payee_id": "pay_mom", "amount_fen": "50000"})
    with pytest.raises(ValidationError):
        api.VerifyRequest(code="12345")


def test_query_models_accept_query_string_text():
    assert api.TransferHistoryQuery.model_validate({"days": "90"}).days == 90
    with pytest.raises(ValidationError):
        api.TransferHistoryQuery.model_validate({"days": "91"})
    fee = api.FeeEstimateQuery.model_validate({"product_id": "990003", "shares": "5000"})
    assert fee.shares == Decimal("5000.0000")


def test_error_body_rejects_floats_in_detail():
    ErrorBody(code=ErrorCode.FEE_CAP, message_zh="手续费超过上限", detail={"fee_fen": 2500})
    with pytest.raises(ValidationError):
        ErrorBody(code=ErrorCode.FEE_CAP, message_zh="手续费超过上限", detail={"fee": [25.0]})
