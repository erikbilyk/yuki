"""Agent tools: arguments, results, read scopes and the schemas sent to the model (SPEC §18.2).

Every argument model forbids unknown fields, so an argument such as `user_confirmed`
fails validation before any core call. No tool approves, verifies, submits, settles,
unlocks or reaches the simulator; those are human routes only (SPEC §6, §9).

Draft tools that take an amount or a payee also take the exact fragment of the
customer's message it was read from (`amount_quote`, `payee_quote`), so that K2 can
parse it independently and refuse a mismatch or an untrusted source (SPEC §18.3).
Provenance labels are derived on the server from those quotes, never from the model.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any

from pydantic import BaseModel, Field, JsonValue, StrictBool, StringConstraints, model_validator

from wg.contracts._base import ContractModel
from wg.contracts.api import (
    AccountsView,
    ApprovalRequestResult,
    BalanceView,
    BillsView,
    CancelResult,
    CashSnapshotView,
    Category,
    CheckpointDay,
    Count,
    DebitRequestsView,
    DraftResult,
    ExposureBasis,
    ExposureView,
    FeeEstimateView,
    Label,
    MandatesView,
    OperationView,
    PayeesView,
    PlanDraftResult,
    PlanView,
    PositionsView,
    ReceiptView,
    Remark,
    Revision,
    Sector,
    SpendingGroupBy,
    SpendingView,
    StressView,
    SubscriptionSpendView,
    TransactionsView,
    TransferHistoryView,
    TxnId,
    UnresolvedView,
)
from wg.contracts.engines import CandidateKind, CandidateReason, ExposureGroup
from wg.contracts.enums import (
    ActionStatus,
    Channel,
    CommitmentStatus,
    DebitDecision,
    DebitRequestStatus,
    MandateChangeKind,
)
from wg.contracts.errors import ErrorCode
from wg.contracts.ids import (
    AccountId,
    CalcResultId,
    CashSnapshotId,
    CommitmentId,
    DebitRequestId,
    FundCode,
    LogicalActionId,
    MandateRef,
    OperationId,
    PayeeId,
    PlanId,
)
from wg.contracts.money import NonNegativeFen, Pct, PositiveFen, Shares
from wg.contracts.plan import ComposePlanRequest


class Capability(StrEnum):
    READ = "read"
    DRAFT = "draft"


# Declared intents (SPEC §10.3, S5-SPEC §3.7); the S4 intents join only if S4 is built.
class Intent(StrEnum):
    """The customer request this call serves; it limits the data the call may read."""

    BALANCE_QUERY = "balance.query"
    TRANSFER_CREATE = "transfer.create"
    TRANSFER_HISTORY = "transfer.history"
    CASH_ANALYZE = "cash.analyze"
    BILLS_MANAGE = "bills.manage"
    FUNDS_REBALANCE = "funds.rebalance"
    PLAN_MANAGE = "plan.manage"
    SUBSCRIPTIONS_MANAGE = "subscriptions.manage"


class DataSource(StrEnum):
    ACCOUNTS = "accounts"
    PAYEES = "payees"
    TRANSFER_HISTORY = "transfer_history"
    OPERATIONS = "operations"
    CASH_SNAPSHOT = "cash_snapshot"
    COMMITMENTS = "commitments"
    SPENDING = "spending"
    TRANSACTIONS = "transactions"
    UNRESOLVED = "unresolved"
    POSITIONS = "positions"
    PRODUCTS = "products"
    HOLDINGS = "holdings"
    CALC_RESULTS = "calc_results"
    PLANS = "plans"
    MANDATES = "mandates"
    DEBIT_REQUESTS = "debit_requests"
    SUBSCRIPTION_SPEND = "subscription_spend"


_D = DataSource

_CASH_ANALYZE = frozenset(
    {_D.ACCOUNTS, _D.CASH_SNAPSHOT, _D.COMMITMENTS, _D.SPENDING, _D.UNRESOLVED}
)
_FUNDS_REBALANCE = frozenset(
    {
        _D.POSITIONS,
        _D.PRODUCTS,
        _D.HOLDINGS,
        _D.CALC_RESULTS,
        _D.CASH_SNAPSHOT,
        _D.PLANS,
        _D.OPERATIONS,
    }
)

# K3 registry seed: intent -> sources a call under that intent may read (S2 §1, S3 §8).
READ_SCOPES: Mapping[Intent, frozenset[DataSource]] = MappingProxyType(
    {
        Intent.BALANCE_QUERY: frozenset({_D.ACCOUNTS}),
        Intent.TRANSFER_CREATE: frozenset({_D.ACCOUNTS, _D.PAYEES, _D.OPERATIONS}),
        Intent.TRANSFER_HISTORY: frozenset({_D.PAYEES, _D.TRANSFER_HISTORY, _D.OPERATIONS}),
        Intent.CASH_ANALYZE: _CASH_ANALYZE,
        Intent.BILLS_MANAGE: frozenset(
            {
                _D.ACCOUNTS,
                _D.PAYEES,
                _D.OPERATIONS,
                _D.CASH_SNAPSHOT,
                _D.COMMITMENTS,
                _D.TRANSACTIONS,
                _D.UNRESOLVED,
            }
        ),
        Intent.FUNDS_REBALANCE: _FUNDS_REBALANCE,
        # The union of cash.analyze and funds.rebalance plus the payees of listed bills.
        Intent.PLAN_MANAGE: _CASH_ANALYZE | _FUNDS_REBALANCE | {_D.PAYEES},
        # Direct-debit agreements only: no history, positions or payees (S5-SPEC §3.7).
        Intent.SUBSCRIPTIONS_MANAGE: frozenset(
            {_D.MANDATES, _D.DEBIT_REQUESTS, _D.SUBSCRIPTION_SPEND, _D.OPERATIONS}
        ),
    }
)

Quote = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64),
    Field(description="The exact fragment of the customer's message this value was read from."),
]
Days = Annotated[int, Field(strict=True, ge=1, le=90)]


class ToolArgs(ContractModel):
    """Common to every tool: the intent the call serves, checked against READ_SCOPES."""

    intent: Intent


def _paired(value: object, quote: object, name: str) -> None:
    if (value is None) != (quote is None):
        raise ValueError(f"{name} and its quote are given together or not at all")


# S1 tools


class GetAccountsArgs(ToolArgs):
    pass


class GetBalanceArgs(ToolArgs):
    account_id: AccountId


class SearchPayeesArgs(ToolArgs):
    query: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=32)]


class GetTransferHistoryArgs(ToolArgs):
    payee_ref: PayeeId | None = None
    days: Days = 30


class GetOperationStatusArgs(ToolArgs):
    """Exactly one of the two references."""

    logical_action_id: LogicalActionId | None = None
    operation_id: OperationId | None = None

    @model_validator(mode="after")
    def _one_reference(self) -> GetOperationStatusArgs:
        if (self.logical_action_id is None) == (self.operation_id is None):
            raise ValueError("give exactly one of logical_action_id and operation_id")
        return self


class OperationStatusResult(ContractModel):
    logical_action_id: LogicalActionId
    revision: Revision
    action_status: ActionStatus
    operation: OperationView | None = None
    receipt: ReceiptView | None = None


class DraftTransferArgs(ToolArgs):
    """`remark` is the customer's payment note, copied as opaque text."""

    payee_ref: PayeeId
    payee_quote: Quote
    amount_fen: PositiveFen
    amount_quote: Quote
    account_id: AccountId | None = None
    remark: Remark | None = None


class RequestApprovalArgs(ToolArgs):
    logical_action_id: LogicalActionId


class CancelActionArgs(ToolArgs):
    logical_action_id: LogicalActionId


# S2 tools


class GetCashSnapshotArgs(ToolArgs):
    pass


class GetBillsArgs(ToolArgs):
    status: CommitmentStatus | None = None
    horizon_days: Days | None = None


class GetSpendingArgs(ToolArgs):
    days: Days = 30
    group_by: SpendingGroupBy = SpendingGroupBy.CATEGORY


class GetTransactionsArgs(ToolArgs):
    account_id: AccountId | None = None
    days: Days = 30
    category: Category | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 20


class GetUnresolvedArgs(ToolArgs):
    pass


class ProposeBillArgs(ToolArgs):
    """Confirm an existing candidate (`commitment_id`) or propose a new bill by purpose.

    Missing fields of a new bill end in a clarification (`MISSING_SLOT`); the payee must
    already be in the payee book.
    """

    commitment_id: CommitmentId | None = None
    purpose: Label | None = None
    payee_ref: PayeeId | None = None
    payee_quote: Quote | None = None
    amount_fen: PositiveFen | None = None
    amount_quote: Quote | None = None
    due_date: date | None = None

    @model_validator(mode="after")
    def _one_form(self) -> ProposeBillArgs:
        _paired(self.payee_ref, self.payee_quote, "payee_ref")
        _paired(self.amount_fen, self.amount_quote, "amount_fen")
        new_fields = (self.purpose, self.payee_ref, self.amount_fen, self.due_date)
        if self.commitment_id is not None:
            if any(value is not None for value in new_fields):
                raise ValueError("a candidate confirmation takes commitment_id only")
        elif self.purpose is None:
            raise ValueError("a new bill needs a purpose")
        return self


class ProposeReserveChangeArgs(ToolArgs):
    target_fen: NonNegativeFen
    amount_quote: Quote


class ProposeResolutionArgs(ToolArgs):
    txn_id: TxnId
    category: Category


class PayBillArgs(ToolArgs):
    """Without an amount the whole unpaid amount is paid; the payee is always the bill's."""

    commitment_id: CommitmentId
    amount_fen: PositiveFen | None = None
    amount_quote: Quote | None = None
    account_id: AccountId | None = None

    @model_validator(mode="after")
    def _amount_with_quote(self) -> PayBillArgs:
        _paired(self.amount_fen, self.amount_quote, "amount_fen")
        return self


class MarkPaidExternalArgs(ToolArgs):
    commitment_id: CommitmentId


class CancelBillArgs(ToolArgs):
    commitment_id: CommitmentId


# S3 and S6 tools


class GetPortfolioExposureArgs(ToolArgs):
    group: ExposureGroup = ExposureGroup.SECTOR
    key: Label
    target_max_pct: Pct | None = None
    basis: ExposureBasis = ExposureBasis.EXECUTABLE


class GetPositionsArgs(ToolArgs):
    pass


class SearchRebalanceCandidatesArgs(ToolArgs):
    """Limits come with their quotes; strictness ("低于" vs "不超过") is read from the quote."""

    sector: Annotated[
        Sector,
        Field(
            description=(
                "Sector key of the fund taxonomy: lower-case English words, for example"
                " 'technology' for 科技. An unknown key is refused with the known keys."
            )
        ),
    ]
    target_max_pct: Pct
    target_quote: Quote
    checkpoint_day: Annotated[
        CheckpointDay | None,
        Field(
            description=(
                "Calendar days from today to the date the target must hold, not a day of the"
                " month: today 2026-11-02 and '11月7日前' give 5. Omit for the default."
            )
        ),
    ] = None
    fee_cap_fen: NonNegativeFen
    fee_cap_quote: Quote
    turnover_cap_fen: NonNegativeFen | None = None


class CandidateBrief(ContractModel):
    kind: CandidateKind
    sell_product: FundCode | None = None
    buy_product: FundCode | None = None
    gross_fen: NonNegativeFen
    fee_fen: NonNegativeFen | None = None
    purchase_fen: NonNegativeFen
    upper_pct_at_checkpoint: Pct
    passes: StrictBool
    reasons: tuple[CandidateReason, ...] = ()


class CandidateSummary(ContractModel):
    """What the model sees of a candidate search: no weight matrices (SPEC §14.7)."""

    calc_result_id: CalcResultId
    cash_snapshot_id: CashSnapshotId
    start_upper_pct: Pct
    target_max_pct: Pct
    selected: CandidateBrief | None = None
    passing_count: Count
    rejected_counts: dict[CandidateReason, Count] = Field(default_factory=dict)
    alternatives: tuple[CandidateBrief, ...] = ()


class EstimateRedemptionFeeArgs(ToolArgs):
    product_id: FundCode
    shares: Shares


class RunStressArgs(ToolArgs):
    calc_result_id: CalcResultId


class GetPlanStatusArgs(ToolArgs):
    """Without `plan_id`, every open plan of the customer."""

    plan_id: PlanId | None = None


class PlanStatusResult(ContractModel):
    plans: tuple[PlanView, ...]


class DraftRebalancePlanArgs(ToolArgs):
    calc_result_id: CalcResultId
    with_bounds: StrictBool = False


class ComposePlanArgs(ToolArgs, ComposePlanRequest):
    pass


# S5 tools (direct-debit agreements)


class ListMandatesArgs(ToolArgs):
    pass


class GetDebitRequestsArgs(ToolArgs):
    status: DebitRequestStatus | None = None
    mandate_ref: MandateRef | None = None


class GetSubscriptionSpendArgs(ToolArgs):
    days: Days = 90


class DraftMandateChangeArgs(ToolArgs):
    """`merchant_quote` names the agreement in the customer's words; `cap_fen` only for set_cap."""

    mandate_ref: MandateRef
    merchant_quote: Quote
    change: MandateChangeKind
    cap_fen: PositiveFen | None = None
    cap_quote: Quote | None = None

    @model_validator(mode="after")
    def _cap_only_for_set_cap(self) -> DraftMandateChangeArgs:
        _paired(self.cap_fen, self.cap_quote, "cap_fen")
        if (self.change is MandateChangeKind.SET_CAP) != (self.cap_fen is not None):
            raise ValueError("cap_fen and cap_quote are given exactly when the change is set_cap")
        return self


class DraftDebitDecisionArgs(ToolArgs):
    """`decision_quote` is the customer's own words that pay or decline; never merchant text."""

    request_id: DebitRequestId
    decision: DebitDecision
    decision_quote: Quote
    merchant_quote: Quote | None = None


class ToolError(ContractModel):
    """Returned to the model instead of a result when a call is refused."""

    code: ErrorCode
    detail: dict[str, JsonValue] = Field(default_factory=dict)


# Registry metadata


@dataclass(frozen=True)
class ToolSpec:
    name: str
    capability: Capability
    sources: frozenset[DataSource]
    arguments: type[ToolArgs]
    result: type[BaseModel]
    description: str
    channels: frozenset[Channel] = frozenset({Channel.WEB, Channel.IM})


def _tool(
    name: str,
    capability: Capability,
    sources: set[DataSource],
    arguments: type[ToolArgs],
    result: type[BaseModel],
    description: str,
) -> ToolSpec:
    return ToolSpec(name, capability, frozenset(sources), arguments, result, description)


_READ = Capability.READ
_DRAFT = Capability.DRAFT

TOOLS: tuple[ToolSpec, ...] = (
    _tool(
        "get_accounts",
        _READ,
        {_D.ACCOUNTS},
        GetAccountsArgs,
        AccountsView,
        "List the customer's accounts with posted and available balances.",
    ),
    _tool(
        "get_balance",
        _READ,
        {_D.ACCOUNTS},
        GetBalanceArgs,
        BalanceView,
        "Posted, reserved and available balance of one account.",
    ),
    _tool(
        "search_payees",
        _READ,
        {_D.PAYEES},
        SearchPayeesArgs,
        PayeesView,
        "Find payees in the customer's payee book by alias or name.",
    ),
    _tool(
        "get_transfer_history",
        _READ,
        {_D.TRANSFER_HISTORY},
        GetTransferHistoryArgs,
        TransferHistoryView,
        "Transfers of the last days (at most 90), optionally to one payee.",
    ),
    _tool(
        "get_operation_status",
        _READ,
        {_D.OPERATIONS},
        GetOperationStatusArgs,
        OperationStatusResult,
        "Status and receipt of the customer's action or operation.",
    ),
    _tool(
        "draft_transfer",
        _DRAFT,
        {_D.ACCOUNTS, _D.PAYEES},
        DraftTransferArgs,
        DraftResult,
        "Draft a transfer to a payee from the book. Creates no payment and no approval.",
    ),
    _tool(
        "request_approval",
        _DRAFT,
        set(),
        RequestApprovalArgs,
        ApprovalRequestResult,
        "Show the customer the approval card of a draft. Only the customer can approve.",
    ),
    _tool(
        "cancel_action",
        _DRAFT,
        set(),
        CancelActionArgs,
        CancelResult,
        "Cancel an action that has not been submitted.",
    ),
    _tool(
        "get_cash_snapshot",
        _READ,
        {_D.CASH_SNAPSHOT},
        GetCashSnapshotArgs,
        CashSnapshotView,
        "Free money (headroom), protected money, next operation, unpaid bills and details.",
    ),
    _tool(
        "get_bills",
        _READ,
        {_D.COMMITMENTS},
        GetBillsArgs,
        BillsView,
        "Bills with their unpaid amounts and derived status.",
    ),
    _tool(
        "get_spending",
        _READ,
        {_D.SPENDING},
        GetSpendingArgs,
        SpendingView,
        "Spending totals by category or month (at most 90 days); no individual lines.",
    ),
    _tool(
        "get_transactions",
        _READ,
        {_D.TRANSACTIONS},
        GetTransactionsArgs,
        TransactionsView,
        "Individual statement lines; only for a question about a specific transaction.",
    ),
    _tool(
        "get_unresolved",
        _READ,
        {_D.UNRESOLVED},
        GetUnresolvedArgs,
        UnresolvedView,
        "Unresolved statement lines and the bound of their impact.",
    ),
    _tool(
        "propose_bill",
        _DRAFT,
        {_D.COMMITMENTS, _D.PAYEES},
        ProposeBillArgs,
        DraftResult,
        "Draft the confirmation of a bill candidate or a new bill. Pays nothing.",
    ),
    _tool(
        "propose_reserve_change",
        _DRAFT,
        {_D.CASH_SNAPSHOT},
        ProposeReserveChangeArgs,
        DraftResult,
        "Draft a new target for the emergency reserve.",
    ),
    _tool(
        "propose_resolution",
        _DRAFT,
        {_D.UNRESOLVED},
        ProposeResolutionArgs,
        DraftResult,
        "Draft the category of an unresolved statement line.",
    ),
    _tool(
        "pay_bill",
        _DRAFT,
        {_D.COMMITMENTS, _D.ACCOUNTS},
        PayBillArgs,
        DraftResult,
        "Draft a payment of a confirmed bill to its own payee, at most the unpaid amount.",
    ),
    _tool(
        "mark_paid_external",
        _DRAFT,
        {_D.COMMITMENTS},
        MarkPaidExternalArgs,
        DraftResult,
        "Draft marking a bill as paid outside this bank, on the customer's word.",
    ),
    _tool(
        "cancel_bill",
        _DRAFT,
        {_D.COMMITMENTS},
        CancelBillArgs,
        DraftResult,
        "Draft the cancellation of a bill.",
    ),
    _tool(
        "get_portfolio_exposure",
        _READ,
        {_D.POSITIONS, _D.HOLDINGS, _D.CALC_RESULTS},
        GetPortfolioExposureArgs,
        ExposureView,
        "Exposure of the fund portfolio to one sector or issuer, with lower and upper bounds.",
    ),
    _tool(
        "get_positions",
        _READ,
        {_D.POSITIONS, _D.PRODUCTS},
        GetPositionsArgs,
        PositionsView,
        "Fund positions: shares, NAV and value.",
    ),
    _tool(
        "search_rebalance_candidates",
        _READ,
        {_D.POSITIONS, _D.PRODUCTS, _D.HOLDINGS, _D.CASH_SNAPSHOT, _D.CALC_RESULTS},
        SearchRebalanceCandidatesArgs,
        CandidateSummary,
        "Search the fixed grid of fund swaps for the smallest one that meets the limits.",
    ),
    _tool(
        "estimate_redemption_fee",
        _READ,
        {_D.POSITIONS, _D.PRODUCTS},
        EstimateRedemptionFeeArgs,
        FeeEstimateView,
        "Redemption fee of a number of shares from the lots held, or unverified.",
    ),
    _tool(
        "run_stress",
        _READ,
        {_D.CALC_RESULTS},
        RunStressArgs,
        StressView,
        "Losses under the four fixed shocks before and after the selected swap.",
    ),
    _tool(
        "get_plan_status",
        _READ,
        {_D.PLANS},
        GetPlanStatusArgs,
        PlanStatusResult,
        "Nodes, statuses, next operation and cash fields of the customer's plans.",
    ),
    _tool(
        "draft_rebalance_plan",
        _DRAFT,
        {_D.CALC_RESULTS, _D.PLANS},
        DraftRebalancePlanArgs,
        PlanDraftResult,
        "Draft a rebalance plan from the selected swap of a candidate search.",
    ),
    _tool(
        "compose_plan",
        _DRAFT,
        {_D.PLANS},
        ComposePlanArgs,
        PlanDraftResult,
        "Propose one plan from drafts and their dependencies; the server validates it.",
    ),
    _tool(
        "list_mandates",
        _READ,
        {_D.MANDATES},
        ListMandatesArgs,
        MandatesView,
        "The customer's direct-debit agreements: status, cap, expected amount, monthly total.",
    ),
    _tool(
        "get_debit_requests",
        _READ,
        {_D.DEBIT_REQUESTS},
        GetDebitRequestsArgs,
        DebitRequestsView,
        "Merchants' debit requests with status and reason codes; the description is merchant text.",
    ),
    _tool(
        "get_subscription_spend",
        _READ,
        {_D.SUBSCRIPTION_SPEND},
        GetSubscriptionSpendArgs,
        SubscriptionSpendView,
        "Direct debits paid in the last days (at most 90), per agreement.",
    ),
    _tool(
        "draft_mandate_change",
        _DRAFT,
        {_D.MANDATES},
        DraftMandateChangeArgs,
        DraftResult,
        "Draft closing, suspending or resuming an agreement, or a new cap. Changes nothing yet.",
    ),
    _tool(
        "draft_debit_decision",
        _DRAFT,
        {_D.DEBIT_REQUESTS, _D.MANDATES},
        DraftDebitDecisionArgs,
        DraftResult,
        "Draft paying or declining a merchant's request, on the customer's own words.",
    ),
)

TOOLS_BY_NAME: Mapping[str, ToolSpec] = MappingProxyType({tool.name: tool for tool in TOOLS})


def _inline_refs(node: Any, defs: Mapping[str, Any]) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = _inline_refs(copy.deepcopy(defs[ref.removeprefix("#/$defs/")]), defs)
            siblings = {key: _inline_refs(value, defs) for key, value in node.items()}
            del siblings["$ref"]
            return {**target, **siblings}
        return {key: _inline_refs(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    return node


def argument_schema(tool: ToolSpec) -> dict[str, Any]:
    """Self-contained JSON Schema of a tool's arguments (no $defs)."""
    schema = tool.arguments.model_json_schema()
    defs = schema.pop("$defs", {})
    return _inline_refs(schema, defs)


def tool_schemas(channel: Channel | None = None) -> list[dict[str, Any]]:
    """Tool definitions in the chat-completions format, optionally for one channel."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": argument_schema(tool),
            },
        }
        for tool in TOOLS
        if channel is None or channel in tool.channels
    ]
