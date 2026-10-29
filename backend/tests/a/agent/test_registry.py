"""The tool registry: validation, capability, channel and read-scope checks, handlers."""

from __future__ import annotations

import json

import pytest

from wg.agent.registry import ToolContext, ToolRegistry
from wg.contracts.api import DraftResult
from wg.contracts.enums import ActionStatus, Channel
from wg.contracts.errors import ErrorCode
from wg.contracts.tools import TOOLS, Capability, DataSource
from wg.core.k2_context import limit_is_strict
from wg.core.k3_policy import extra_read_count
from wg.k6 import audit

pytestmark = pytest.mark.layer("U")

BALANCE = {"intent": "balance.query"}


def refusals(registry: ToolRegistry, customer_id: str = "cus_main") -> list[tuple[str, str, str]]:
    with registry.db.read() as conn:
        rows = audit.list_audit(conn, customer_id=customer_id)
    return [(row.event, row.ref or "", row.decision or "") for row in rows]


def agent_operations(conn) -> int:
    """Operations made for an action; the base profile also holds some of its own."""
    query = "SELECT COUNT(*) FROM operations WHERE logical_action_id IS NOT NULL"
    return conn.execute(query).fetchone()[0]


def transfer_args(**extra) -> dict:
    return {
        "intent": "transfer.create",
        "payee_ref": "pay_mom",
        "payee_quote": "妈妈",
        "amount_fen": 50_000,
        "amount_quote": "500元",
        **extra,
    }


def test_every_tool_has_a_handler_and_none_can_approve_or_settle(registry: ToolRegistry):
    assert {tool.name for tool in TOOLS} == set(registry._handlers)
    names = " ".join(tool.name for tool in TOOLS)
    for forbidden in ("approve", "confirm", "verify", "submit", "settle", "unlock", "sim"):
        assert forbidden not in names


def test_a_read_tool_returns_the_service_result(registry: ToolRegistry, context: ToolContext):
    outcome = registry.call(context, "get_accounts", BALANCE)
    assert outcome.ok and outcome.result is not None
    payload = json.loads(outcome.payload())
    assert {account["account_id"] for account in payload["accounts"]} >= {"acc_main"}


def test_an_unknown_tool_argument_is_rejected_and_audited(
    registry: ToolRegistry, context: ToolContext
):
    outcome = registry.call(context, "draft_transfer", transfer_args(user_confirmed=True))
    assert outcome.error is not None and outcome.error.code is ErrorCode.UNSUPPORTED_REQUEST
    assert outcome.error.detail == {"fields": ["user_confirmed"]}
    assert ("tool_refused", "draft_transfer", "UNSUPPORTED_REQUEST") in refusals(registry)
    with registry.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_missing_intent_and_float_amounts_are_rejected(
    registry: ToolRegistry, context: ToolContext
):
    assert registry.call(context, "get_accounts", {}).error is not None
    outcome = registry.call(context, "draft_transfer", transfer_args(amount_fen=500.5))
    assert outcome.error is not None and outcome.error.detail == {"fields": ["amount_fen"]}


def test_refusal_detail_never_carries_the_rejected_values(
    registry: ToolRegistry, context: ToolContext
):
    outcome = registry.call(context, "draft_transfer", transfer_args(remark=["secret-remark"]))
    assert outcome.error is not None
    assert "secret-remark" not in outcome.payload()
    audited = " ".join(" ".join(row) for row in refusals(registry))
    assert "secret-remark" not in audited


def test_an_unknown_tool_is_refused_and_audited(registry: ToolRegistry, context: ToolContext):
    outcome = registry.call(context, "approve_action", {"intent": "transfer.create"})
    assert outcome.error is not None and outcome.error.detail == {"reason": "unknown_tool"}
    assert ("tool_refused", "approve_action", "UNSUPPORTED_REQUEST") in refusals(registry)


def test_a_context_without_the_draft_capability_cannot_draft(registry: ToolRegistry):
    read_only = ToolContext(
        customer_id="cus_main",
        channel=Channel.WEB,
        capabilities=frozenset({Capability.READ}),
        utterance="给妈妈转500元",
    )
    outcome = registry.call(read_only, "draft_transfer", transfer_args())
    assert outcome.error is not None and outcome.error.code is ErrorCode.FORBIDDEN_ROUTE
    assert outcome.error.detail == {"reason": "capability"}
    assert ("tool_refused", "draft_transfer", "FORBIDDEN_ROUTE") in refusals(registry)
    with registry.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    names = {schema["function"]["name"] for schema in registry.schemas(read_only)}
    assert "get_accounts" in names and "draft_transfer" not in names


def test_schemas_are_filtered_by_channel(registry: ToolRegistry, context: ToolContext):
    names = {schema["function"]["name"] for schema in registry.schemas(context)}
    assert names == {tool.name for tool in TOOLS}


def test_a_tool_outside_the_context_channel_is_refused(
    registry: ToolRegistry, context: ToolContext, monkeypatch: pytest.MonkeyPatch
):
    from dataclasses import replace

    from wg.agent import registry as registry_module
    from wg.contracts import tools

    web_only = replace(tools.TOOLS_BY_NAME["get_accounts"], channels=frozenset({Channel.WEB}))
    patched = tuple(web_only if tool.name == "get_accounts" else tool for tool in TOOLS)
    monkeypatch.setattr(tools, "TOOLS", patched)
    monkeypatch.setattr(registry_module, "TOOLS_BY_NAME", {t.name: t for t in patched})
    im = ToolContext(customer_id="cus_main", channel=Channel.IM)
    outcome = registry.call(im, "get_accounts", BALANCE)
    assert outcome.error is not None and outcome.error.detail == {"reason": "channel"}
    assert "get_accounts" not in {s["function"]["name"] for s in registry.schemas(im)}
    assert registry.call(context, "get_accounts", BALANCE).ok


def test_a_read_outside_the_declared_intent_is_refused_and_counted(
    registry: ToolRegistry, context: ToolContext
):
    with registry.db.read() as conn:
        before = extra_read_count(conn, "cus_main")
    outcome = registry.call(context, "get_cash_snapshot", BALANCE)
    assert outcome.error is not None and outcome.error.code is ErrorCode.READ_SCOPE_DENIED
    with registry.db.read() as conn:
        assert extra_read_count(conn, "cus_main") == before + 1


def test_the_context_read_scope_limits_sources_further(registry: ToolRegistry):
    narrow = ToolContext(
        customer_id="cus_main",
        channel=Channel.WEB,
        read_scope=frozenset({DataSource.PAYEES}),
    )
    outcome = registry.call(narrow, "get_accounts", BALANCE)
    assert outcome.error is not None and outcome.error.code is ErrorCode.READ_SCOPE_DENIED
    assert outcome.error.detail["sources"] == ["accounts"]
    with registry.db.read() as conn:
        assert extra_read_count(conn, "cus_main") == 1


def test_a_foreign_object_is_not_found_for_the_agent_too(
    registry: ToolRegistry, context: ToolContext
):
    other = registry.call(context, "get_balance", {**BALANCE, "account_id": "acc_other_main"})
    missing = registry.call(context, "get_balance", {**BALANCE, "account_id": "acc_nope"})
    assert other.error is not None and other.error.code is ErrorCode.NOT_FOUND
    assert other.payload() == missing.payload()


def test_draft_transfer_with_a_quote_from_the_message_makes_a_draft_only(
    registry: ToolRegistry, context: ToolContext
):
    outcome = registry.call(context, "draft_transfer", transfer_args())
    assert outcome.ok and isinstance(outcome.result, DraftResult)
    assert outcome.result.status is ActionStatus.DRAFT
    with registry.db.read() as conn:
        assert agent_operations(conn) == 0
        row = conn.execute("SELECT status, provenance_json FROM actions").fetchone()
    assert row["status"] == "DRAFT"
    assert json.loads(row["provenance_json"])["amount_fen"]["quote"] == "500元"


def test_draft_transfer_with_a_quote_not_in_the_message_is_refused(
    registry: ToolRegistry, context: ToolContext
):
    outcome = registry.call(context, "draft_transfer", transfer_args(amount_quote="5000元"))
    assert outcome.error is not None and outcome.error.code is ErrorCode.UNTRUSTED_SOURCE
    assert outcome.error.detail == {"slots": ["amount_fen"]}
    with registry.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_request_approval_shows_the_card_but_approves_nothing(
    registry: ToolRegistry, context: ToolContext
):
    draft = registry.call(context, "draft_transfer", transfer_args())
    assert isinstance(draft.result, DraftResult)
    outcome = registry.call(
        context,
        "request_approval",
        {"intent": "transfer.create", "logical_action_id": draft.result.logical_action_id},
    )
    assert outcome.ok
    with registry.db.read() as conn:
        assert (
            conn.execute("SELECT status FROM actions").fetchone()["status"] == "AWAITING_APPROVAL"
        )
        assert agent_operations(conn) == 0
    status = registry.call(
        context,
        "get_operation_status",
        {"intent": "transfer.history", "logical_action_id": draft.result.logical_action_id},
    )
    assert status.ok and json.loads(status.payload())["action_status"] == "AWAITING_APPROVAL"


def test_cancel_action_cancels_a_draft(registry: ToolRegistry, context: ToolContext):
    draft = registry.call(context, "draft_transfer", transfer_args())
    assert isinstance(draft.result, DraftResult)
    outcome = registry.call(
        context,
        "cancel_action",
        {"intent": "transfer.create", "logical_action_id": draft.result.logical_action_id},
    )
    assert outcome.ok
    with registry.db.read() as conn:
        assert conn.execute("SELECT status FROM actions").fetchone()["status"] == "CANCELLED"


def test_a_new_bill_with_missing_fields_ends_in_a_clarification(
    registry: ToolRegistry, context: ToolContext
):
    outcome = registry.call(context, "propose_bill", {"intent": "bills.manage", "purpose": "房租"})
    assert outcome.error is not None and outcome.error.code is ErrorCode.MISSING_SLOT
    assert outcome.error.detail == {"slots": ["payee_ref", "amount_fen", "due_date"]}
    assert ("tool_refused", "propose_bill", "MISSING_SLOT") in refusals(registry)


def test_a_reserve_amount_must_come_from_the_message(registry: ToolRegistry, context: ToolContext):
    outcome = registry.call(
        context,
        "propose_reserve_change",
        {"intent": "bills.manage", "target_fen": 3_000_000, "amount_quote": "3万"},
    )
    assert outcome.error is not None and outcome.error.code is ErrorCode.UNTRUSTED_SOURCE


def test_get_plan_status_without_a_plan_lists_no_open_plans(
    registry: ToolRegistry, context: ToolContext
):
    outcome = registry.call(context, "get_plan_status", {"intent": "plan.manage"})
    assert outcome.ok and json.loads(outcome.payload()) == {"plans": []}


@pytest.mark.parametrize(
    ("quote", "strict"),
    [
        ("不超过35%", False),
        ("35%", False),
        ("低于35%", True),
        ("少于200元", True),
        ("不超过50元", False),
        ("< 35%", True),
    ],
)
def test_limit_strictness_is_read_from_the_quote(quote: str, strict: bool):
    assert limit_is_strict(quote) is strict
