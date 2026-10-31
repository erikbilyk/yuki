import json

import pytest
from pydantic import ValidationError

from wg.contracts.enums import Channel
from wg.contracts.tools import (
    READ_SCOPES,
    TOOLS,
    TOOLS_BY_NAME,
    Capability,
    DataSource,
    GetOperationStatusArgs,
    Intent,
    PayBillArgs,
    ProposeBillArgs,
    tool_schemas,
)

# SPEC §18.2 core tools and the direct-debit tools of S5-SPEC §8.1.
SPEC_READ = (
    "get_accounts get_balance search_payees get_transfer_history get_operation_status "
    "get_cash_snapshot get_bills get_spending get_transactions get_unresolved "
    "get_portfolio_exposure get_positions search_rebalance_candidates estimate_redemption_fee "
    "run_stress get_plan_status "
    "list_mandates get_debit_requests get_subscription_spend"
).split()
SPEC_DRAFT = (
    "draft_transfer request_approval cancel_action propose_bill propose_reserve_change "
    "propose_resolution pay_bill mark_paid_external cancel_bill draft_rebalance_plan compose_plan "
    "draft_mandate_change draft_debit_decision"
).split()

# A minimal valid argument object per tool.
VALID_ARGS = {
    "get_accounts": {"intent": "balance.query"},
    "get_balance": {"intent": "balance.query", "account_id": "acc_main"},
    "search_payees": {"intent": "transfer.create", "query": "妈妈"},
    "get_transfer_history": {"intent": "transfer.history", "days": 30},
    "get_operation_status": {"intent": "transfer.create", "logical_action_id": "act_1"},
    "draft_transfer": {
        "intent": "transfer.create",
        "payee_ref": "pay_mom",
        "payee_quote": "妈妈",
        "amount_fen": 50000,
        "amount_quote": "500",
    },
    "request_approval": {"intent": "transfer.create", "logical_action_id": "act_1"},
    "cancel_action": {"intent": "transfer.create", "logical_action_id": "act_1"},
    "get_cash_snapshot": {"intent": "cash.analyze"},
    "get_bills": {"intent": "bills.manage"},
    "get_spending": {"intent": "cash.analyze"},
    "get_transactions": {"intent": "bills.manage"},
    "get_unresolved": {"intent": "cash.analyze"},
    "propose_bill": {"intent": "bills.manage", "commitment_id": "cmt_new"},
    "propose_reserve_change": {
        "intent": "bills.manage",
        "target_fen": 500000,
        "amount_quote": "5000",
    },
    "propose_resolution": {
        "intent": "bills.manage",
        "txn_id": "MAIN-202610-007",
        "category": "family_transfer",
    },
    "pay_bill": {"intent": "bills.manage", "commitment_id": "cmt_rent"},
    "mark_paid_external": {"intent": "bills.manage", "commitment_id": "cmt_insurance"},
    "cancel_bill": {"intent": "bills.manage", "commitment_id": "cmt_insurance"},
    "get_portfolio_exposure": {"intent": "funds.rebalance", "key": "technology"},
    "get_positions": {"intent": "funds.rebalance"},
    "search_rebalance_candidates": {
        "intent": "funds.rebalance",
        "sector": "technology",
        "target_max_pct": "40",
        "target_quote": "不超过40%",
        "fee_cap_fen": 2000,
        "fee_cap_quote": "20元",
    },
    "estimate_redemption_fee": {
        "intent": "funds.rebalance",
        "product_id": "990003",
        "shares": "5000",
    },
    "run_stress": {"intent": "funds.rebalance", "calc_result_id": "clc_1"},
    "get_plan_status": {"intent": "plan.manage"},
    "draft_rebalance_plan": {"intent": "funds.rebalance", "calc_result_id": "clc_1"},
    "compose_plan": {
        "intent": "plan.manage",
        "draft_ids": ["act_redeem", "act_buy"],
        "depends_on": {"act_buy": ["act_redeem"]},
    },
    "list_mandates": {"intent": "subscriptions.manage"},
    "get_debit_requests": {"intent": "subscriptions.manage", "status": "AWAITING_CLIENT"},
    "get_subscription_spend": {"intent": "subscriptions.manage", "days": 90},
    "draft_mandate_change": {
        "intent": "subscriptions.manage",
        "mandate_ref": "mdt_video",
        "merchant_quote": "视频会员",
        "change": "cancel",
    },
    "draft_debit_decision": {
        "intent": "subscriptions.manage",
        "request_id": "req_1",
        "decision": "pay",
        "decision_quote": "付款",
    },
}

FORBIDDEN_WORDS = ("approve", "confirm", "verify", "submit", "settle", "unlock", "sim")


def test_tool_names_and_capabilities_equal_the_spec():
    assert [tool.name for tool in TOOLS] == list(TOOLS_BY_NAME)
    read = {tool.name for tool in TOOLS if tool.capability is Capability.READ}
    draft = {tool.name for tool in TOOLS if tool.capability is Capability.DRAFT}
    assert read == set(SPEC_READ)
    assert draft == set(SPEC_DRAFT)
    assert set(VALID_ARGS) == set(TOOLS_BY_NAME)


@pytest.mark.parametrize("tool", TOOLS, ids=lambda tool: tool.name)
def test_every_tool_rejects_unknown_arguments(tool):
    args = VALID_ARGS[tool.name]
    tool.arguments.model_validate(args)
    for extra in ("user_confirmed", "customer_id", "approved"):
        with pytest.raises(ValidationError) as error:
            tool.arguments.model_validate({**args, extra: True})
        assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("tool", TOOLS, ids=lambda tool: tool.name)
def test_every_tool_requires_an_intent(tool):
    args = dict(VALID_ARGS[tool.name])
    del args["intent"]
    with pytest.raises(ValidationError):
        tool.arguments.model_validate(args)
    with pytest.raises(ValidationError):
        tool.arguments.model_validate({**args, "intent": "subscriptions.cancel"})


@pytest.mark.parametrize("tool", TOOLS, ids=lambda tool: tool.name)
def test_argument_schemas_are_closed_and_self_contained(tool):
    (schema,) = [s["function"] for s in tool_schemas() if s["function"]["name"] == tool.name]
    parameters = schema["parameters"]
    assert parameters["type"] == "object"
    assert parameters["additionalProperties"] is False
    assert "intent" in parameters["required"]
    text = json.dumps(parameters)
    assert "$ref" not in text and "$defs" not in text
    assert schema["description"]


def test_no_tool_can_approve_or_control_the_simulator():
    for tool in TOOLS:
        assert not any(word in tool.name for word in FORBIDDEN_WORDS), tool.name
        for field in tool.arguments.model_fields:
            assert field not in ("user_confirmed", "customer_id", "code", "terms_hash"), field


def test_amounts_and_payees_carry_their_quotes():
    drafting = [tool for tool in TOOLS if tool.capability is Capability.DRAFT]
    for tool in drafting:
        fields = tool.arguments.model_fields
        if "amount_fen" in fields or "target_fen" in fields:
            assert "amount_quote" in fields, tool.name
        if "payee_ref" in fields:
            assert "payee_quote" in fields, tool.name
    candidates = TOOLS_BY_NAME["search_rebalance_candidates"].arguments.model_fields
    assert {"target_quote", "fee_cap_quote"} <= set(candidates)


def test_quotes_are_paired_with_their_values():
    PayBillArgs(
        intent="bills.manage", commitment_id="cmt_rent", amount_fen=100000, amount_quote="1000"
    )
    with pytest.raises(ValidationError):
        PayBillArgs(intent="bills.manage", commitment_id="cmt_rent", amount_fen=100000)
    with pytest.raises(ValidationError):
        PayBillArgs(intent="bills.manage", commitment_id="cmt_rent", amount_quote="1000")
    with pytest.raises(ValidationError):
        ProposeBillArgs(intent="bills.manage", purpose="幼儿园", payee_ref="pay_wangfang")
    with pytest.raises(ValidationError):
        PayBillArgs(
            intent="bills.manage", commitment_id="cmt_rent", amount_fen=1000.0, amount_quote="10"
        )


def test_propose_bill_forms():
    ProposeBillArgs(intent="bills.manage", purpose="幼儿园学费")  # missing slots: clarification
    ProposeBillArgs(
        intent="bills.manage",
        purpose="幼儿园学费",
        payee_ref="pay_wangfang",
        payee_quote="王芳",
        amount_fen=120000,
        amount_quote="1200",
        due_date="2026-11-09",
    )
    with pytest.raises(ValidationError):
        ProposeBillArgs(intent="bills.manage")
    with pytest.raises(ValidationError):
        ProposeBillArgs(intent="bills.manage", commitment_id="cmt_new", purpose="幼儿园学费")


def test_operation_status_takes_exactly_one_reference():
    GetOperationStatusArgs(intent="transfer.create", operation_id="opn_1")
    with pytest.raises(ValidationError):
        GetOperationStatusArgs(intent="transfer.create")
    with pytest.raises(ValidationError):
        GetOperationStatusArgs(
            intent="transfer.create", operation_id="opn_1", logical_action_id="act_1"
        )


def test_read_scopes_cover_every_tool():
    assert set(READ_SCOPES) == set(Intent)
    for tool in TOOLS:
        assert any(tool.sources <= scope for scope in READ_SCOPES.values()), tool.name
    plan = READ_SCOPES[Intent.PLAN_MANAGE]
    assert READ_SCOPES[Intent.CASH_ANALYZE] | READ_SCOPES[Intent.FUNDS_REBALANCE] <= plan
    assert DataSource.PAYEES in plan
    # A balance question reads neither history nor fund positions (T16, T31).
    for intent in (Intent.BALANCE_QUERY, Intent.CASH_ANALYZE):
        assert DataSource.TRANSFER_HISTORY not in READ_SCOPES[intent]
        assert DataSource.TRANSACTIONS not in READ_SCOPES[intent]
        assert DataSource.POSITIONS not in READ_SCOPES[intent]


def test_the_subscription_intent_reads_agreements_and_nothing_else():
    new = {DataSource.MANDATES, DataSource.DEBIT_REQUESTS, DataSource.SUBSCRIPTION_SPEND}
    scope = READ_SCOPES[Intent.SUBSCRIPTIONS_MANAGE]
    assert scope == new | {DataSource.OPERATIONS}
    for other in set(Intent) - {Intent.SUBSCRIPTIONS_MANAGE}:
        assert not new & READ_SCOPES[other], other
    assert not scope & {
        DataSource.TRANSFER_HISTORY,
        DataSource.TRANSACTIONS,
        DataSource.POSITIONS,
        DataSource.PAYEES,
        DataSource.ACCOUNTS,
    }


def test_schemas_per_channel_serialize():
    every = tool_schemas()
    assert len(every) == len(TOOLS)
    assert {s["function"]["name"] for s in tool_schemas(Channel.IM)} <= set(TOOLS_BY_NAME)
    assert len(tool_schemas(Channel.WEB)) == len(TOOLS)
    json.dumps(every, ensure_ascii=False)
