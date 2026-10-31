"""Request and response bodies of the HTTP API and the route table (SPEC §6, §19).

Money is integer fen and Decimals travel as strings, as everywhere in the contracts.
Query models use lax integers because query strings arrive as text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from wg.contracts._base import ContractModel
from wg.contracts.engines import (
    BillState,
    CandidateSearchResult,
    ExposureGroup,
    ExposureResult,
    FeeQuote,
    PlanBounds,
    Sha256Hex,
    SnapshotQuality,
    StressResult,
)
from wg.contracts.enums import (
    ActionKind,
    ActionStatus,
    AuditActor,
    BillStatus,
    Channel,
    CheckpointResult,
    CommitmentStatus,
    DebitDecision,
    DebitReason,
    DebitRequestStatus,
    Disposition,
    EffectCheck,
    Fault,
    LockScope,
    MandateChangeKind,
    MandateStatus,
    OperationStatus,
    PayeeBank,
    PlanKind,
    PlanMode,
    PlanStatus,
    Provenance,
    ReceiptStatus,
    Recurrence,
    Role,
    Tier,
)

# ErrorBody is the body of every error response of every route.
from wg.contracts.errors import ErrorBody as ErrorBody
from wg.contracts.errors import ErrorCode
from wg.contracts.facts import FactBlock
from wg.contracts.ids import (
    AccountId,
    ApprovalId,
    CalcResultId,
    CardId,
    CashSnapshotId,
    ChallengeId,
    CommitmentId,
    CustomerId,
    DebitRequestId,
    EventId,
    FundCode,
    LockId,
    LogicalActionId,
    MandateRef,
    MerchantId,
    OperationId,
    OperatorId,
    PayeeId,
    PlanId,
    PurposeId,
    ReceiptId,
    StatementId,
)
from wg.contracts.money import (
    DecimalStr,
    Fen,
    Nav,
    NonNegativeFen,
    Pct,
    PositiveFen,
    Rate,
    Shares,
)
from wg.contracts.plan import ComposePlanRequest, PlanValidationResult

Revision = Annotated[int, Field(strict=True, ge=1)]
Count = Annotated[int, Field(strict=True, ge=0)]
Text = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Label = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Code6 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9]{6}$")]
Category = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,31}$")]
Sector = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,31}$")]
# Statement lines keep the source's transaction ID (for example "MAIN-202611-003").
TxnId = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80)]
Remark = Annotated[str, StringConstraints(max_length=64)]
CheckpointDay = Annotated[int, Field(strict=True, ge=1, le=8)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
DebitDay = Annotated[int, Field(strict=True, ge=1, le=31)]
# A CST calendar month, the period of a direct-debit request (S5-SPEC §5.1).
PeriodKey = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
MerchantText = Annotated[str, StringConstraints(max_length=140)]
SimText = Annotated[str, StringConstraints(min_length=1, max_length=64)]

# Session (SPEC §6)


class LoginRequest(ContractModel):
    handle: Label
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class SessionEntry(ContractModel):
    """One logged-in role in this browser; `subject_id` is the customer or operator."""

    role: Role
    subject_id: CustomerId | OperatorId
    display_name: str | None = None
    csrf_token: Annotated[str, StringConstraints(min_length=16, max_length=128)]
    expires_at: AwareDatetime


class SessionView(ContractModel):
    sessions: tuple[SessionEntry, ...]


class LogoutRequest(ContractModel):
    """Ends the session of one role; the request carries that role's CSRF token."""

    role: Role


# Reads: accounts, payees, transfers, operations, receipts (S1)


class AccountView(ContractModel):
    """`available_fen` is posted money minus active reservations on the account."""

    account_id: AccountId
    name: Label
    currency: Literal["CNY"] = "CNY"
    posted_fen: Fen
    available_fen: Fen
    covered: StrictBool


class AccountsView(ContractModel):
    accounts: tuple[AccountView, ...]


class BalanceView(ContractModel):
    account_id: AccountId
    name: Label
    posted_fen: Fen
    reserved_fen: NonNegativeFen
    available_fen: Fen
    as_of: AwareDatetime


class PayeesQuery(ContractModel):
    q: Annotated[str, StringConstraints(max_length=32)] | None = None


class PayeeView(ContractModel):
    """A payee as the model and the UI see it: handle, names, bank and the masked account."""

    payee_id: PayeeId
    alias: Label
    display_name: Label
    bank: PayeeBank
    account_masked: Label


class PayeesView(ContractModel):
    payees: tuple[PayeeView, ...]


class TransferHistoryQuery(ContractModel):
    payee_id: PayeeId | None = None
    days: Annotated[int, Field(ge=1, le=90)] = 30


class TransferView(ContractModel):
    operation_id: OperationId
    logical_action_id: LogicalActionId
    kind: Literal[ActionKind.TRANSFER, ActionKind.PAY_BILL]
    account_id: AccountId
    payee_id: PayeeId
    payee_display: Label
    amount_fen: PositiveFen
    commitment_id: CommitmentId | None = None
    status: OperationStatus
    submitted_at: AwareDatetime
    settled_at: AwareDatetime | None = None
    receipt_id: ReceiptId | None = None


class TransferHistoryView(ContractModel):
    days: Annotated[int, Field(strict=True, ge=1, le=90)]
    transfers: tuple[TransferView, ...]


class OperationView(ContractModel):
    """The operation for one logical action at one revision (one per idempotency key)."""

    operation_id: OperationId
    logical_action_id: LogicalActionId
    revision: Revision
    plan_id: PlanId | None = None
    kind: ActionKind
    status: OperationStatus
    account_id: AccountId
    amount_fen: NonNegativeFen | None = None
    shares: Shares | None = None
    product_id: FundCode | None = None
    payee_id: PayeeId | None = None
    commitment_id: CommitmentId | None = None
    submitted_at: AwareDatetime
    settle_due_at: AwareDatetime | None = None
    settled_at: AwareDatetime | None = None
    receipt_id: ReceiptId | None = None


class ReceiptView(ContractModel):
    """The single immutable receipt of a terminal outcome (SPEC §12.5)."""

    receipt_id: ReceiptId
    operation_id: OperationId
    logical_action_id: LogicalActionId
    revision: Revision
    status: ReceiptStatus
    kind: ActionKind
    account_id: AccountId
    amount_fen: NonNegativeFen | None = None
    fee_fen: NonNegativeFen | None = None
    net_fen: NonNegativeFen | None = None
    shares: Shares | None = None
    product_id: FundCode | None = None
    payee_id: PayeeId | None = None
    payee_display: Label | None = None
    commitment_id: CommitmentId | None = None
    unpaid_after_fen: NonNegativeFen | None = None
    reason_code: ErrorCode | None = None
    created_at: AwareDatetime
    # Direct-debit kinds only; effect_check only for the changes that are probed (S5-SPEC §5.7).
    mandate_ref: MandateRef | None = None
    request_id: DebitRequestId | None = None
    merchant_display: Label | None = None
    effect_check: EffectCheck | None = None

    @model_validator(mode="after")
    def _reason_only_when_rejected(self) -> ReceiptView:
        if (self.status is ReceiptStatus.REJECTED) != (self.reason_code is not None):
            raise ValueError("a receipt has a reason code exactly when it is REJECTED")
        return self


# Cash and bills (S2)


class NodePhrase(StrEnum):
    """Key of the plain phrase shown for a node or the next operation (S6 §8)."""

    SCHEDULED = "scheduled"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CODE = "awaiting_code"
    READY = "ready"
    SUBMITTED = "submitted"
    CHECKING = "checking"
    DONE = "done"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    NEEDS_DECISION = "needs_decision"
    SUPERSEDED = "superseded"


_A = ActionStatus

NODE_PHRASE: Mapping[ActionStatus, NodePhrase] = MappingProxyType(
    {
        _A.DRAFT: NodePhrase.SCHEDULED,
        _A.AWAITING_APPROVAL: NodePhrase.AWAITING_APPROVAL,
        _A.AWAITING_STEPUP: NodePhrase.AWAITING_CODE,
        _A.READY: NodePhrase.READY,
        _A.SUBMITTED: NodePhrase.SUBMITTED,
        _A.UNKNOWN: NodePhrase.CHECKING,
        _A.SETTLED: NodePhrase.DONE,
        _A.REJECTED: NodePhrase.REJECTED,
        _A.CANCELLED: NodePhrase.CANCELLED,
        _A.PAUSED: NodePhrase.NEEDS_DECISION,
        _A.SUPERSEDED: NodePhrase.SUPERSEDED,
    }
)


class NextOperationView(ContractModel):
    logical_action_id: LogicalActionId
    plan_id: PlanId | None = None
    kind: ActionKind
    status: ActionStatus
    phrase: NodePhrase
    amount_fen: NonNegativeFen | None = None
    shares: Shares | None = None
    not_before: AwareDatetime | None = None


class CashMode(StrEnum):
    """`shortfall` replaces 可自由支配 with "缺口 ¥X" on the customer screen."""

    HEADROOM = "headroom"
    SHORTFALL = "shortfall"


class CashFields(ContractModel):
    """The four persistent customer fields, all from one snapshot (SPEC §20)."""

    snapshot_id: CashSnapshotId
    as_of: AwareDatetime
    mode: CashMode
    headroom_fen: NonNegativeFen
    shortfall_fen: NonNegativeFen
    protected_fen: NonNegativeFen
    next_operation: NextOperationView | None = None
    unpaid_bills_fen: NonNegativeFen
    unpaid_bills_count: Count

    @model_validator(mode="after")
    def _mode_follows_shortfall(self) -> CashFields:
        if self.headroom_fen > 0 and self.shortfall_fen > 0:
            raise ValueError("headroom and shortfall cannot both be positive")
        if (self.mode is CashMode.SHORTFALL) != (self.shortfall_fen > 0):
            raise ValueError("mode is shortfall exactly when shortfall_fen > 0")
        return self


class AccountAvailability(ContractModel):
    """Per-account bank availability; `reconciled` is None for uncovered accounts."""

    account_id: AccountId
    name: Label
    covered: StrictBool
    posted_fen: Fen
    available_fen: Fen
    reconciled: StrictBool | None = None


class SubscriptionPeriodState(ContractModel):
    """A protected direct-debit period inside a cash snapshot (S5-SPEC §5.6)."""

    mandate_ref: MandateRef
    merchant_display: Label
    period_key: PeriodKey
    due_date: date
    expected_fen: PositiveFen
    status: Literal[BillStatus.SECURED, BillStatus.UNSECURED, BillStatus.OVERDUE]


class CashDetails(ContractModel):
    """计算明细: the formula inputs, per-account availability, monthly estimate, quality."""

    rules_version: Label
    c_fen: Fen
    e_fen: NonNegativeFen
    l_fen: NonNegativeFen
    b_fen: NonNegativeFen
    m_fen: NonNegativeFen
    u_fen: NonNegativeFen
    accounts: tuple[AccountAvailability, ...]
    monthly_estimate_fen: Fen
    bills: tuple[BillState, ...]
    quality: SnapshotQuality
    subscriptions: tuple[SubscriptionPeriodState, ...] = ()


class CashSnapshotView(CashFields):
    details: CashDetails


class SourceLabel(ContractModel):
    """Where one slot's value came from, with the zh-CN label shown next to it."""

    slot: Label
    provenance: Provenance
    label: Text


class BillsQuery(ContractModel):
    status: CommitmentStatus | None = None
    horizon_days: Annotated[int, Field(ge=1, le=90)] | None = None


class BillView(ContractModel):
    """A commitment; `derived` is the snapshot status for unpaid confirmed bills."""

    commitment_id: CommitmentId
    purpose_id: PurposeId
    kind: Label
    payee_id: PayeeId
    payee_display: Label
    amount_fen: NonNegativeFen
    unpaid_fen: NonNegativeFen
    due_date: date
    recurrence: Recurrence
    status: CommitmentStatus
    derived: BillStatus | None = None
    source: Label
    sources: tuple[SourceLabel, ...] = ()


class BillsView(ContractModel):
    snapshot_id: CashSnapshotId
    bills: tuple[BillView, ...]


class SpendingGroupBy(StrEnum):
    CATEGORY = "category"
    MONTH = "month"


class SpendingQuery(ContractModel):
    days: Annotated[int, Field(ge=1, le=90)] = 30
    group_by: SpendingGroupBy = SpendingGroupBy.CATEGORY


class SpendingRow(ContractModel):
    key: Label
    spent_fen: NonNegativeFen
    count: Count


class SpendingView(ContractModel):
    """Aggregates only; individual lines come from /v1/transactions."""

    from_date: date
    to_date: date
    group_by: SpendingGroupBy
    rows: tuple[SpendingRow, ...]
    total_spent_fen: NonNegativeFen


class TransactionsQuery(ContractModel):
    account_id: AccountId | None = None
    days: Annotated[int, Field(ge=1, le=90)] = 30
    category: Category | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 50


class TransactionView(ContractModel):
    """An imported statement line; the `_raw` fields are untrusted text."""

    txn_id: TxnId
    account_id: AccountId
    posted_on: date
    amount_fen: Fen
    counterparty_raw: str
    description_raw: str
    category: Category | None = None
    resolved: StrictBool


class TransactionsView(ContractModel):
    transactions: tuple[TransactionView, ...]


class UnresolvedItem(ContractModel):
    txn_id: TxnId
    account_id: AccountId
    posted_on: date
    amount_fen: Fen
    description_raw: str
    impact_fen: NonNegativeFen


class UnresolvedView(ContractModel):
    snapshot_id: CashSnapshotId
    items: tuple[UnresolvedItem, ...]
    count: Count
    impact_fen: NonNegativeFen


# Funds (S3)


class PercentQuality(StrEnum):
    """估算 for projections, 已确认 for values measured on settled positions."""

    ESTIMATED = "estimated"
    CONFIRMED = "confirmed"


class ExposureBasis(StrEnum):
    """`public` results are dated diagnostics and never back an order (SPEC §22)."""

    EXECUTABLE = "executable"
    PUBLIC = "public"


class ExposureQuery(ContractModel):
    group: ExposureGroup = ExposureGroup.SECTOR
    key: Label
    target_max_pct: Pct | None = None
    basis: ExposureBasis = ExposureBasis.EXECUTABLE


class ExposureView(ContractModel):
    calc_result_id: CalcResultId
    basis: ExposureBasis
    quality: PercentQuality
    exposure: ExposureResult


class PublicFeeTier(ContractModel):
    """Redemption fee for a holding period of `from_days` to `to_days` whole days (open if None)."""

    from_days: NonNegativeInt
    to_days: NonNegativeInt | None
    rate: Rate


class PublicFund(ContractModel):
    product_id: FundCode
    name: Label
    share_class: Label
    net_assets_fen: NonNegativeFen
    redemption_tiers: tuple[PublicFeeTier, ...]
    report_title: Text
    report_sha256: Sha256Hex
    source_url: Text | None


class PublicOverlap(ContractModel):
    """`overlap_percent` is in percent (26.709382 means 26.709382 %), unlike `Pct` fractions."""

    definition: Text
    shared_securities: Count
    overlap_percent: DecimalStr(6)


class PublicDiagnosticsView(ContractModel):
    """Dated figures from the funds' interim reports; they never back an order (SPEC §22)."""

    calc_result_id: CalcResultId
    period_end: date
    retrieved_on: date
    executable: Literal[False]
    funds: tuple[PublicFund, ...]
    overlap: PublicOverlap


class PositionView(ContractModel):
    """`free_shares` excludes shares already in a pending redemption."""

    product_id: FundCode
    name: Label
    share_class: Label
    shares: Shares
    free_shares: Shares
    nav: Nav
    nav_date: date
    value_fen: NonNegativeFen


class PositionsView(ContractModel):
    as_of: AwareDatetime
    positions: tuple[PositionView, ...]
    total_fen: NonNegativeFen


class CandidatesRequest(ContractModel):
    """Search inputs from the guided form; unset caps and dates take the plan constants."""

    sector: Sector
    target_max_pct: Pct
    target_inclusive: StrictBool = True
    checkpoint_day: CheckpointDay | None = None
    fee_cap_fen: NonNegativeFen
    fee_cap_inclusive: StrictBool = True
    turnover_cap_fen: NonNegativeFen | None = None


class CandidatesView(ContractModel):
    calc_result_id: CalcResultId
    result: CandidateSearchResult


class StressRequest(ContractModel):
    """Stress the selected candidate of a candidate search result."""

    calc_result_id: CalcResultId


class StressView(ContractModel):
    calc_result_id: CalcResultId
    candidates_result_id: CalcResultId
    result: StressResult


class FeeEstimateQuery(ContractModel):
    product_id: FundCode
    shares: Shares


class FeeEstimateView(ContractModel):
    product_id: FundCode
    shares: Shares
    quote: FeeQuote


# Plans (S3, S6)


class RevisionReason(StrEnum):
    INITIAL = "initial"
    BILL_CHANGE = "bill_change"
    NEW_APPROVAL = "new_approval"


class PlanNodeView(ContractModel):
    """One logical action at its active revision; `receipt_id` links the receipt."""

    logical_action_id: LogicalActionId
    revision: Revision
    kind: ActionKind
    status: ActionStatus
    phrase: NodePhrase
    tier: Tier
    depends_on: tuple[LogicalActionId, ...] = ()
    not_before: AwareDatetime | None = None
    due_date: date | None = None
    amount_fen: NonNegativeFen | None = None
    shares: Shares | None = None
    product_id: FundCode | None = None
    payee_id: PayeeId | None = None
    payee_display: Label | None = None
    commitment_id: CommitmentId | None = None
    pause_reasons: tuple[ErrorCode, ...] = ()
    challenge_expires_at: AwareDatetime | None = None
    card_id: CardId | None = None
    operation_id: OperationId | None = None
    receipt_id: ReceiptId | None = None
    submitted_at: AwareDatetime | None = None
    settled_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _reasons_only_when_paused(self) -> PlanNodeView:
        if (self.status is ActionStatus.PAUSED) != bool(self.pause_reasons):
            raise ValueError("a node has pause reasons exactly when it is PAUSED")
        return self


class PlanRevisionView(ContractModel):
    revision: Revision
    reason: RevisionReason
    trigger_event_id: EventId | None = None
    cash_snapshot_id: CashSnapshotId
    calc_result_id: CalcResultId | None = None
    approval_id: ApprovalId | None = None
    within_bounds: StrictBool
    purchase_before_fen: NonNegativeFen | None = None
    purchase_after_fen: NonNegativeFen | None = None
    created_at: AwareDatetime


class CheckpointView(ContractModel):
    """The single T+5 checkpoint row of a plan (SPEC §15.6)."""

    logical_action_id: LogicalActionId
    result: CheckpointResult
    measured_pct: Pct
    lower_pct: Pct
    upper_pct: Pct
    target_max_pct: Pct
    quality: PercentQuality
    evaluated_at: AwareDatetime


class PlanSummary(ContractModel):
    plan_id: PlanId
    kind: PlanKind
    status: PlanStatus
    mode: PlanMode
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    next_operation: NextOperationView | None = None


class PlanView(PlanSummary):
    active_revision: Revision
    root_approval_id: ApprovalId | None = None
    card_id: CardId | None = None
    bounds: PlanBounds | None = None
    nodes: tuple[PlanNodeView, ...]
    revisions: tuple[PlanRevisionView, ...] = ()
    checkpoint: CheckpointView | None = None
    cash: CashFields | None = None


class PlansView(ContractModel):
    plans: tuple[PlanSummary, ...]


# Drafts (guided UI) and their results (SPEC §9.1)


class DraftResult(ContractModel):
    """A draft grants nothing; `feasible` holds exactly when `reasons` is empty."""

    logical_action_id: LogicalActionId
    revision: Revision
    kind: ActionKind
    status: ActionStatus
    tier: Tier
    feasible: StrictBool
    reasons: tuple[ErrorCode, ...] = ()
    commitment_id: CommitmentId | None = None

    @model_validator(mode="after")
    def _feasible_without_reasons(self) -> DraftResult:
        if self.feasible == bool(self.reasons):
            raise ValueError("a draft is feasible exactly when it has no reasons")
        return self


class PlanDraftResult(ContractModel):
    """Result of drafting or composing a plan; a plan exists exactly when validation passed."""

    plan_id: PlanId | None = None
    kind: PlanKind
    validation: PlanValidationResult
    actions: tuple[DraftResult, ...] = ()

    @model_validator(mode="after")
    def _plan_only_when_valid(self) -> PlanDraftResult:
        if self.validation.ok != (self.plan_id is not None):
            raise ValueError("plan_id is set exactly when validation passed")
        return self


class TransferDraftRequest(ContractModel):
    """`account_id` None means the customer's main account; the remark is opaque text."""

    payee_id: PayeeId
    amount_fen: PositiveFen
    account_id: AccountId | None = None
    remark: Remark | None = None


class BillDraftRequest(ContractModel):
    """Confirm an existing candidate (`commitment_id`) or enter a new bill with every field."""

    commitment_id: CommitmentId | None = None
    purpose: Label | None = None
    payee_id: PayeeId | None = None
    amount_fen: PositiveFen | None = None
    due_date: date | None = None

    @model_validator(mode="after")
    def _one_form(self) -> BillDraftRequest:
        new_fields = (self.purpose, self.payee_id, self.amount_fen, self.due_date)
        if self.commitment_id is not None:
            if any(value is not None for value in new_fields):
                raise ValueError("a candidate confirmation takes commitment_id only")
        elif any(value is None for value in new_fields):
            raise ValueError("a new bill needs purpose, payee_id, amount_fen and due_date")
        return self


class ReserveDraftRequest(ContractModel):
    target_fen: NonNegativeFen


class ResolutionDraftRequest(ContractModel):
    txn_id: TxnId
    category: Category


class PayBillDraftRequest(ContractModel):
    """`amount_fen` None pays the whole unpaid amount; the payee is always the bill's."""

    commitment_id: CommitmentId
    amount_fen: PositiveFen | None = None
    account_id: AccountId | None = None


class CommitmentDraftRequest(ContractModel):
    """Body of mark-paid-external and cancel-bill."""

    commitment_id: CommitmentId


class RebalancePlanDraftRequest(ContractModel):
    """Draft the selected swap of a candidate search; bounds come from the plan constants."""

    calc_result_id: CalcResultId
    with_bounds: StrictBool = False


class ApprovalRequestResult(ContractModel):
    """The card is now visible in the trusted UI; nothing is approved."""

    logical_action_id: LogicalActionId
    revision: Revision
    status: ActionStatus
    tier: Tier
    card_id: CardId


class CancelResult(ContractModel):
    logical_action_id: LogicalActionId
    revision: Revision
    status: ActionStatus
    reservation_released: StrictBool


# Trusted routes (SPEC §9)


class CardKind(StrEnum):
    ACTION = "action"
    PLAN = "plan"
    PLAN_REVISION = "plan_revision"


class CardStatus(StrEnum):
    OPEN = "open"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class CardLine(ContractModel):
    """One rendered zh-CN line; its numbers come from the snapshot or calc result cited."""

    key: Label
    label: Label
    text: Text


class MoneyChange(ContractModel):
    unit: Literal["fen"] = "fen"
    key: Label
    label: Label
    before: Fen | None = None
    after: Fen


class PercentChange(ContractModel):
    unit: Literal["percent"] = "percent"
    key: Label
    label: Label
    before: Pct | None = None
    after: Pct
    quality: PercentQuality


CardChange = Annotated[MoneyChange | PercentChange, Field(discriminator="unit")]


class Card(ContractModel):
    """A server-rendered approval card; the client confirms by sending back `terms_hash`."""

    card_id: CardId
    kind: CardKind
    status: CardStatus
    title: Text
    tier: Literal[Tier.GREEN, Tier.YELLOW, Tier.RED]
    requires_code: StrictBool
    logical_action_id: LogicalActionId | None = None
    revision: Revision | None = None
    action_kind: ActionKind | None = None
    plan_id: PlanId | None = None
    parent_approval_id: ApprovalId | None = None
    approval_id: ApprovalId | None = None
    lines: tuple[CardLine, ...]
    changes: tuple[CardChange, ...] = ()
    sources: tuple[SourceLabel, ...] = ()
    warnings: tuple[Text, ...] = ()
    cash_snapshot_id: CashSnapshotId | None = None
    calc_result_id: CalcResultId | None = None
    terms_hash: Sha256Hex
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _consistent(self) -> Card:
        if self.requires_code != (self.tier is Tier.RED):
            raise ValueError("a card requires a code exactly when its tier is RED")
        if (self.status is CardStatus.CONFIRMED) != (self.approval_id is not None):
            raise ValueError("approval_id is set exactly when the card is confirmed")
        if self.kind is CardKind.ACTION:
            if None in (self.logical_action_id, self.revision, self.action_kind):
                raise ValueError("an action card names its action, revision and kind")
        elif self.plan_id is None:
            raise ValueError("a plan card names its plan")
        if (self.kind is CardKind.PLAN_REVISION) != (self.parent_approval_id is not None):
            raise ValueError("parent_approval_id is set exactly on a plan revision card")
        return self


class ConfirmRequest(ContractModel):
    terms_hash: Sha256Hex


class ChallengeView(ContractModel):
    """A step-up challenge; the code itself appears only in the simulated SMS panel."""

    challenge_id: ChallengeId
    logical_action_id: LogicalActionId
    revision: Revision
    expires_at: AwareDatetime
    attempts_left: Annotated[int, Field(strict=True, ge=0, le=3)]


class ConfirmResult(ContractModel):
    card_id: CardId
    approval_id: ApprovalId
    plan_id: PlanId | None = None
    logical_action_id: LogicalActionId | None = None
    action_status: ActionStatus | None = None
    challenge: ChallengeView | None = None
    operation: OperationView | None = None


class VerifyRequest(ContractModel):
    code: Code6


class VerifyResult(ContractModel):
    challenge_id: ChallengeId
    logical_action_id: LogicalActionId
    revision: Revision
    action_status: ActionStatus
    operation: OperationView | None = None


class SmsMessage(ContractModel):
    """An open code in the simulated phone of the session's customer."""

    challenge_id: ChallengeId
    logical_action_id: LogicalActionId
    kind: ActionKind
    amount_fen: NonNegativeFen | None = None
    code: Code6
    created_at: AwareDatetime
    expires_at: AwareDatetime


class SmsView(ContractModel):
    messages: tuple[SmsMessage, ...]


class PairingCodeView(ContractModel):
    code: Code6
    expires_at: AwareDatetime


# Chat (SPEC §17, §18.4)


class ChatRequest(ContractModel):
    text: Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class ChatReply(ContractModel):
    """Fact block rendered by the server plus the model's checked explanation, if any."""

    disposition: Disposition | None = None
    facts: FactBlock
    fact_lines: tuple[str, ...]
    explanation: str | None = None
    explanation_replaced: StrictBool
    card_ids: tuple[CardId, ...] = ()
    logical_action_ids: tuple[LogicalActionId, ...] = ()
    plan_id: PlanId | None = None


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatHistoryQuery(ContractModel):
    limit: Annotated[int, Field(ge=1, le=200)] = 50


class ChatMessageView(ContractModel):
    """Stored chat text is masked (SPEC §18.6)."""

    role: ChatRole
    channel: Channel
    text: str
    explanation_replaced: StrictBool
    created_at: AwareDatetime


class ChatHistoryView(ContractModel):
    messages: tuple[ChatMessageView, ...]


# Audit and operator (SPEC §16)


class AuditQuery(ContractModel):
    plan_id: PlanId | None = None
    limit: Annotated[int, Field(ge=1, le=500)] = 200


class OperatorAuditQuery(AuditQuery):
    customer_id: CustomerId | None = None


class AuditLine(ContractModel):
    """One audit row and its plain zh-CN line; never model reasoning, hashes or JSON."""

    at: AwareDatetime
    actor: AuditActor
    event: Label
    ref: Label | None = None
    decision: Label | None = None
    reason: Label | None = None
    text: Text


class AuditView(ContractModel):
    lines: tuple[AuditLine, ...]


class LocksQuery(ContractModel):
    active_only: bool = True


class LockView(ContractModel):
    lock_id: LockId
    scope: LockScope
    scope_ref: Label
    reason: Label
    since: AwareDatetime
    cleared_at: AwareDatetime | None = None
    cleared_by: OperatorId | None = None
    clear_reason: Text | None = None


class LocksView(ContractModel):
    locks: tuple[LockView, ...]


class UnlockRequest(ContractModel):
    reason: Text


class ReconcileRequest(ContractModel):
    """Reconcile SUBMITTED and UNKNOWN operations, optionally for one customer or plan."""

    customer_id: CustomerId | None = None
    plan_id: PlanId | None = None


class ReconcileResult(ContractModel):
    checked: Count
    resolved: Count
    operations: tuple[OperationView, ...]


# Direct-debit agreements (S5-SPEC §3.5)

_REASONED_REQUEST_STATUSES = frozenset(
    {
        DebitRequestStatus.AUTO_DECLINED,
        DebitRequestStatus.OUT_OF_MANDATE,
        DebitRequestStatus.EXPIRED,
    }
)


class MerchantView(ContractModel):
    merchant_id: MerchantId
    display_name: Label


class MerchantsView(ContractModel):
    """Verified merchants only."""

    merchants: tuple[MerchantView, ...]


class ProtectionPeriodView(ContractModel):
    """`until` is when the protection ends, if known (cancelled agreement, declined request)."""

    period_key: PeriodKey
    due_date: date
    expected_fen: PositiveFen
    until: AwareDatetime | None = None


class MandateView(ContractModel):
    """One agreement; `expected_fen` is the current expected amount of a period."""

    mandate_ref: MandateRef
    merchant_id: MerchantId
    merchant_display: Label
    account_id: AccountId
    status: MandateStatus
    expected_fen: PositiveFen
    cap_fen: PositiveFen
    period: Literal["monthly"]
    debit_day: DebitDay
    next_debit_date: date | None = None
    starts_on: date
    expires_on: date
    version: Revision
    protection: tuple[ProtectionPeriodView, ...] = ()
    pending_requests: Count


class MandatesView(ContractModel):
    """`monthly_total_fen` sums `expected_fen` of the ACTIVE agreements."""

    as_of: AwareDatetime
    monthly_total_fen: NonNegativeFen
    mandates: tuple[MandateView, ...]


class DebitRequestView(ContractModel):
    """A merchant's debit request; `description_raw` is the merchant's untrusted text."""

    request_id: DebitRequestId
    mandate_ref: MandateRef
    merchant_id: MerchantId
    merchant_display: Label
    amount_fen: PositiveFen
    currency: Literal["CNY"]
    period_key: PeriodKey
    status: DebitRequestStatus
    reason_code: DebitReason | None = None
    received_at: AwareDatetime
    respond_by: AwareDatetime
    description_raw: MerchantText
    payable: StrictBool
    operation_id: OperationId | None = None

    @model_validator(mode="after")
    def _reason_and_payable_follow_status(self) -> DebitRequestView:
        if (self.status in _REASONED_REQUEST_STATUSES) != (self.reason_code is not None):
            raise ValueError(
                "reason_code is set exactly for AUTO_DECLINED, OUT_OF_MANDATE and EXPIRED"
            )
        if self.payable and self.status is not DebitRequestStatus.AWAITING_CLIENT:
            raise ValueError("only a request awaiting the customer can be payable")
        return self


class DebitRequestsQuery(ContractModel):
    status: DebitRequestStatus | None = None
    mandate_ref: MandateRef | None = None


class DebitRequestsView(ContractModel):
    """The customer's own requests, newest first; probes are never listed."""

    requests: tuple[DebitRequestView, ...]


class SubscriptionSpendQuery(ContractModel):
    days: Annotated[int, Field(ge=1, le=90)] = 90


class SubscriptionSpendRow(ContractModel):
    mandate_ref: MandateRef
    merchant_display: Label
    paid_count: Count
    paid_fen: NonNegativeFen
    last_paid_on: date | None = None
    last_amount_fen: PositiveFen | None = None


class SubscriptionSpendView(ContractModel):
    """Paid requests only."""

    days: Annotated[int, Field(strict=True, ge=1, le=90)]
    rows: tuple[SubscriptionSpendRow, ...]
    total_fen: NonNegativeFen


class MandateChangeRequest(ContractModel):
    """`cap_fen` is given exactly when the change is `set_cap`."""

    mandate_ref: MandateRef
    change: MandateChangeKind
    cap_fen: PositiveFen | None = None

    @model_validator(mode="after")
    def _cap_only_for_set_cap(self) -> MandateChangeRequest:
        if (self.change is MandateChangeKind.SET_CAP) != (self.cap_fen is not None):
            raise ValueError("cap_fen is given exactly when the change is set_cap")
        return self


class DebitDecisionRequest(ContractModel):
    request_id: DebitRequestId
    decision: DebitDecision


class MandateCreateRequest(ContractModel):
    merchant_id: MerchantId
    expected_fen: PositiveFen
    cap_fen: PositiveFen
    debit_day: DebitDay
    months: Annotated[int, Field(strict=True, ge=1, le=12)]

    @model_validator(mode="after")
    def _cap_covers_expected(self) -> MandateCreateRequest:
        if self.cap_fen < self.expected_fen:
            raise ValueError("cap_fen must not be below expected_fen")
        return self


# Simulator (demo mode, SPEC §12.7, §15.3)


class SimResetRequest(ContractModel):
    profile: Label = "fixture_v1"
    variant: Label | None = None


class ClockView(ContractModel):
    """`day` is the simulated day number n of T+n, counted from the epoch's date."""

    now: AwareDatetime
    epoch: AwareDatetime
    day: NonNegativeInt


class SimResetResult(ContractModel):
    profile: Label
    variant: Label | None = None
    clock: ClockView


class AgentModeRequest(ContractModel):
    """The agent mode for every current customer session (SPEC §26); the reply has no body."""

    mode: PlanMode


class ClockAdvanceRequest(ContractModel):
    """Either an absolute target `to` or a relative step of `days` and `minutes`."""

    to: AwareDatetime | None = None
    days: NonNegativeInt | None = None
    minutes: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _one_form(self) -> ClockAdvanceRequest:
        relative = self.days is not None or self.minutes is not None
        if relative == (self.to is not None):
            raise ValueError("give either `to` or `days`/`minutes`")
        return self


class FaultRequest(ContractModel):
    fault: Fault
    count: Annotated[int, Field(strict=True, ge=1)] = 1
    persistent: StrictBool = False


class FaultView(ContractModel):
    fault: Fault
    remaining_count: NonNegativeInt
    persistent: StrictBool
    armed_at: AwareDatetime


class FaultsView(ContractModel):
    faults: tuple[FaultView, ...]


class ImportRequest(ContractModel):
    statement_id: StatementId


class ImportResult(ContractModel):
    """Counts of one import; a repeated statement adds nothing and keeps the snapshot."""

    statement_id: StatementId
    new_transactions: Count
    duplicate_transactions: Count
    new_commitments: Count
    snapshot_id: CashSnapshotId
    inputs_hash: Sha256Hex


class EbillRequest(ContractModel):
    """A trusted e-bill; it becomes a CANDIDATE that the customer confirms (CONFIRM_BILL)."""

    customer_id: CustomerId
    payee_id: PayeeId
    kind: Label
    amount_fen: PositiveFen
    due_date: date


class SimCommitmentResult(ContractModel):
    commitment_id: CommitmentId
    status: CommitmentStatus


class SimSmsRequest(ContractModel):
    """Untrusted SMS text; it may prefill a candidate card but never sets a field."""

    customer_id: CustomerId
    text: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class SimSmsResult(ContractModel):
    commitment_id: CommitmentId | None = None


class IncomingPaymentRequest(ContractModel):
    account_id: AccountId
    amount_fen: PositiveFen
    payer_name: Label
    remark: Annotated[str, StringConstraints(max_length=140)] = ""


class ExternalDebitRequest(ContractModel):
    """A debit the bank did not originate through WealthGuard (T69)."""

    account_id: AccountId
    amount_fen: PositiveFen
    merchant_raw: Label


class SimDebitRequest(ContractModel):
    """A merchant's debit request as the simulator receives it (S5-SPEC §5.2).

    Plain strings and any integer on purpose: a malformed amount or currency must reach the
    request check, which refuses and audits it as FORMAT_ERROR.
    """

    merchant_id: SimText
    merchant_request_id: SimText
    mandate_ref: SimText
    amount_fen: StrictInt
    currency: Annotated[str, StringConstraints(min_length=1, max_length=8)]
    description_raw: MerchantText = ""


class SimDebitResult(ContractModel):
    request_id: DebitRequestId
    status: DebitRequestStatus
    reason_code: DebitReason | None = None
    respond_by: AwareDatetime
    duplicate: StrictBool


class SimPostingResult(ContractModel):
    """`amount_fen` is signed: positive for money in, negative for money out."""

    operation_id: OperationId
    account_id: AccountId
    amount_fen: Fen
    posted_fen_after: Fen


# IM channel adapter (SPEC §21)


class FeishuMessage(ContractModel):
    message_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    open_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    chat_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    text: Annotated[str, StringConstraints(max_length=2000)]


class ChannelReply(ContractModel):
    """Reply text for the adapter to send; `links` point to trusted web cards."""

    message_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    duplicate: StrictBool
    bound: StrictBool
    text: str
    links: tuple[str, ...] = ()


# Health


class HealthView(ContractModel):
    status: Literal["ok"]
    build: Label
    demo: StrictBool
    features: tuple[Label, ...] = ()


# Route table (SPEC §19.2)


class RouteRole(StrEnum):
    """Who may call a route. `session` is any logged-in role; `channel` is the IM adapter."""

    PUBLIC = "public"
    SESSION = "session"
    CUSTOMER = "customer"
    OPERATOR = "operator"
    DEMO = "demo"
    CHANNEL = "channel"


class RouteGroup(StrEnum):
    HEALTH = "health"
    SESSION = "session"
    READS = "reads"
    DRAFTS = "drafts"
    TRUSTED = "trusted"
    CHAT = "chat"
    OPERATOR = "operator"
    SIMULATOR = "simulator"
    CHANNEL = "channel"


@dataclass(frozen=True)
class Route:
    """One route: who may call it, whether it needs X-CSRF-Token, and its bodies.

    `response` is the success body; None when the route answers without one (/sim/crash
    ends the process, /sim/agent-mode answers 204).
    """

    method: Literal["GET", "POST"]
    path: str
    role: RouteRole
    csrf: bool
    group: RouteGroup
    response: type[BaseModel] | None
    request: type[BaseModel] | None = None
    query: type[BaseModel] | None = None

    @property
    def demo_only(self) -> bool:
        return self.group is RouteGroup.SIMULATOR


_G = RouteGroup
_R = RouteRole


def _get(
    path: str,
    role: RouteRole,
    group: RouteGroup,
    response: type[BaseModel],
    query: type[BaseModel] | None = None,
) -> Route:
    return Route("GET", path, role, False, group, response, query=query)


def _post(
    path: str,
    role: RouteRole,
    group: RouteGroup,
    response: type[BaseModel] | None,
    request: type[BaseModel] | None = None,
) -> Route:
    # Every state-changing human route needs the CSRF token of its role (SPEC §6).
    csrf = role not in (_R.PUBLIC, _R.CHANNEL)
    return Route("POST", path, role, csrf, group, response, request=request)


ROUTES: tuple[Route, ...] = (
    _get("/healthz", _R.PUBLIC, _G.HEALTH, HealthView),
    # Session
    _post("/v1/auth/login", _R.PUBLIC, _G.SESSION, SessionEntry, LoginRequest),
    _post("/v1/auth/logout", _R.SESSION, _G.SESSION, SessionView, LogoutRequest),
    _get("/v1/session", _R.PUBLIC, _G.SESSION, SessionView),
    # Reads
    _get("/v1/accounts", _R.CUSTOMER, _G.READS, AccountsView),
    _get("/v1/accounts/{id}/balance", _R.CUSTOMER, _G.READS, BalanceView),
    _get("/v1/payees", _R.CUSTOMER, _G.READS, PayeesView, PayeesQuery),
    _get("/v1/transfers/history", _R.CUSTOMER, _G.READS, TransferHistoryView, TransferHistoryQuery),
    _get("/v1/operations/{id}", _R.CUSTOMER, _G.READS, OperationView),
    _get("/v1/receipts/{operation_id}", _R.CUSTOMER, _G.READS, ReceiptView),
    _get("/v1/cash/snapshot", _R.CUSTOMER, _G.READS, CashSnapshotView),
    _get("/v1/bills", _R.CUSTOMER, _G.READS, BillsView, BillsQuery),
    _get("/v1/spending", _R.CUSTOMER, _G.READS, SpendingView, SpendingQuery),
    _get("/v1/transactions", _R.CUSTOMER, _G.READS, TransactionsView, TransactionsQuery),
    _get("/v1/unresolved", _R.CUSTOMER, _G.READS, UnresolvedView),
    _get("/v1/funds/exposure", _R.CUSTOMER, _G.READS, ExposureView, ExposureQuery),
    _get("/v1/funds/positions", _R.CUSTOMER, _G.READS, PositionsView),
    _get("/v1/funds/public-diagnostics", _R.CUSTOMER, _G.READS, PublicDiagnosticsView),
    _post("/v1/funds/candidates", _R.CUSTOMER, _G.READS, CandidatesView, CandidatesRequest),
    _post("/v1/funds/stress", _R.CUSTOMER, _G.READS, StressView, StressRequest),
    _get("/v1/funds/fees/estimate", _R.CUSTOMER, _G.READS, FeeEstimateView, FeeEstimateQuery),
    _get("/v1/plans", _R.CUSTOMER, _G.READS, PlansView),
    _get("/v1/plans/{id}", _R.CUSTOMER, _G.READS, PlanView),
    _get("/v1/audit", _R.CUSTOMER, _G.READS, AuditView, AuditQuery),
    # Drafts (guided UI)
    _post("/v1/drafts/transfer", _R.CUSTOMER, _G.DRAFTS, DraftResult, TransferDraftRequest),
    _post("/v1/drafts/bill", _R.CUSTOMER, _G.DRAFTS, DraftResult, BillDraftRequest),
    _post("/v1/drafts/reserve", _R.CUSTOMER, _G.DRAFTS, DraftResult, ReserveDraftRequest),
    _post("/v1/drafts/resolution", _R.CUSTOMER, _G.DRAFTS, DraftResult, ResolutionDraftRequest),
    _post("/v1/drafts/pay-bill", _R.CUSTOMER, _G.DRAFTS, DraftResult, PayBillDraftRequest),
    _post(
        "/v1/drafts/mark-paid-external",
        _R.CUSTOMER,
        _G.DRAFTS,
        DraftResult,
        CommitmentDraftRequest,
    ),
    _post("/v1/drafts/cancel-bill", _R.CUSTOMER, _G.DRAFTS, DraftResult, CommitmentDraftRequest),
    _post("/v1/funds/plans", _R.CUSTOMER, _G.DRAFTS, PlanDraftResult, RebalancePlanDraftRequest),
    _post("/v1/plans/compose", _R.CUSTOMER, _G.DRAFTS, PlanDraftResult, ComposePlanRequest),
    _post("/v1/actions/{id}/request-approval", _R.CUSTOMER, _G.DRAFTS, ApprovalRequestResult),
    _post("/v1/actions/{id}/cancel", _R.CUSTOMER, _G.DRAFTS, CancelResult),
    # Trusted (human only)
    _get("/v1/cards/{id}", _R.CUSTOMER, _G.TRUSTED, Card),
    _post("/v1/cards/{id}/confirm", _R.CUSTOMER, _G.TRUSTED, ConfirmResult, ConfirmRequest),
    _post("/v1/actions/{id}/challenge", _R.CUSTOMER, _G.TRUSTED, ChallengeView),
    _post("/v1/challenges/{id}/verify", _R.CUSTOMER, _G.TRUSTED, VerifyResult, VerifyRequest),
    _post("/v1/challenges/{id}/reissue", _R.CUSTOMER, _G.TRUSTED, ChallengeView),
    _post("/v1/plans/{id}/stop", _R.CUSTOMER, _G.TRUSTED, PlanView),
    _get("/v1/sms", _R.CUSTOMER, _G.TRUSTED, SmsView),
    _post("/v1/channels/pairing-code", _R.CUSTOMER, _G.TRUSTED, PairingCodeView),
    # Chat
    _post("/v1/chat/messages", _R.CUSTOMER, _G.CHAT, ChatReply, ChatRequest),
    _get("/v1/chat/history", _R.CUSTOMER, _G.CHAT, ChatHistoryView, ChatHistoryQuery),
    # Operator
    _get("/v1/operator/locks", _R.OPERATOR, _G.OPERATOR, LocksView, LocksQuery),
    _post("/v1/operator/locks/{id}/unlock", _R.OPERATOR, _G.OPERATOR, LockView, UnlockRequest),
    _post("/v1/operator/reconcile", _R.OPERATOR, _G.OPERATOR, ReconcileResult, ReconcileRequest),
    _get("/v1/operator/audit", _R.OPERATOR, _G.OPERATOR, AuditView, OperatorAuditQuery),
    # Simulator (WG_DEMO_MODE=1)
    _post("/sim/reset", _R.DEMO, _G.SIMULATOR, SimResetResult, SimResetRequest),
    _post("/sim/clock/advance", _R.DEMO, _G.SIMULATOR, ClockView, ClockAdvanceRequest),
    _post("/sim/faults", _R.DEMO, _G.SIMULATOR, FaultsView, FaultRequest),
    _post("/sim/import", _R.DEMO, _G.SIMULATOR, ImportResult, ImportRequest),
    _post("/sim/bills/ebill", _R.DEMO, _G.SIMULATOR, SimCommitmentResult, EbillRequest),
    _post("/sim/sms", _R.DEMO, _G.SIMULATOR, SimSmsResult, SimSmsRequest),
    _post("/sim/incoming-payment", _R.DEMO, _G.SIMULATOR, SimPostingResult, IncomingPaymentRequest),
    _post("/sim/external-debit", _R.DEMO, _G.SIMULATOR, SimPostingResult, ExternalDebitRequest),
    _post("/sim/crash", _R.DEMO, _G.SIMULATOR, None),
    _post("/sim/agent-mode", _R.DEMO, _G.SIMULATOR, None, AgentModeRequest),
    # IM channel adapter
    _post("/v1/channels/feishu/messages", _R.CHANNEL, _G.CHANNEL, ChannelReply, FeishuMessage),
)


def find_route(method: str, path: str) -> Route:
    """The route with this method and path template, such as ("GET", "/v1/plans/{id}")."""
    for route in ROUTES:
        if route.method == method and route.path == path:
            return route
    raise KeyError(f"{method} {path}")


def route_models() -> tuple[type[BaseModel], ...]:
    """Every request, query and response model used by a route, each once."""
    seen: dict[type[BaseModel], None] = {}
    for route in ROUTES:
        for model in (route.request, route.query, route.response):
            if model is not None:
                seen.setdefault(model, None)
    return tuple(seen)
