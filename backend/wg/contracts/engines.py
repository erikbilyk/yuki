"""Inputs, results and call signatures of the pure cash and funds engines (SPEC §13.2, §14).

The engines live in `wg.numeric`; these types are their contract with the core.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import AwareDatetime, Field, StrictBool, StringConstraints, model_validator

from wg.contracts._base import ContractModel
from wg.contracts.enums import BillStatus, CommitmentStatus
from wg.contracts.errors import ErrorCode
from wg.contracts.ids import (
    AccountId,
    CashSnapshotId,
    CommitmentId,
    FeeScheduleId,
    FundCode,
    LotId,
    OperationId,
    PayeeId,
    PurposeId,
)
from wg.contracts.money import Fen, Nav, NonNegativeFen, Pct, PositiveFen, Rate, Shares, Weight

Sha256Hex = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]

# Cash engine (SPEC §13.2)


class CashAccountInput(ContractModel):
    """One account: posted balance and the reconciliation of its imported statements."""

    account_id: AccountId
    posted_fen: Fen
    covered: StrictBool
    opening_fen: Fen
    imported_sum_fen: Fen
    closing_fen: Fen


class CommitmentInput(ContractModel):
    commitment_id: CommitmentId
    purpose_id: PurposeId
    status: CommitmentStatus
    amount_fen: NonNegativeFen
    unpaid_fen: NonNegativeFen
    due_date: date
    payee_id: PayeeId


class HoldInput(ContractModel):
    """An outgoing hold; a hold sharing a bill's purpose_id is already counted in B."""

    operation_id: OperationId
    purpose_id: PurposeId | None
    amount_fen: NonNegativeFen


class UnresolvedInput(ContractModel):
    txn_id: str
    amount_fen: Fen


class AssumptionKind(StrEnum):
    PAID_REPORTED = "paid_reported"


class Assumption(ContractModel):
    """A fact the snapshot takes on the customer's word, such as a bill paid elsewhere."""

    kind: AssumptionKind
    ref: str


class CashInputs(ContractModel):
    as_of: AwareDatetime
    rules_version: str
    accounts: tuple[CashAccountInput, ...]
    reserve_fen: NonNegativeFen
    allowance_remaining_fen: NonNegativeFen
    margin_fen: NonNegativeFen
    commitments: tuple[CommitmentInput, ...]
    holds: tuple[HoldInput, ...]
    unresolved: tuple[UnresolvedInput, ...]
    income_this_month_fen: NonNegativeFen
    spent_this_month_fen: NonNegativeFen
    horizon_days: NonNegativeInt = 30
    assumptions: tuple[Assumption, ...] = ()


class BillState(ContractModel):
    commitment_id: CommitmentId
    unpaid_fen: NonNegativeFen
    due_date: date
    status: BillStatus


class SnapshotQuality(ContractModel):
    as_of: AwareDatetime
    uncovered_accounts: tuple[AccountId, ...]
    unresolved_count: NonNegativeInt
    unresolved_impact_fen: Fen
    reconciled: dict[AccountId, StrictBool]
    assumptions: tuple[Assumption, ...]
    incomplete: StrictBool


class CashSnapshotValues(ContractModel):
    """C, E, L, B, M, U and the derived amounts of one snapshot (SPEC §13.1)."""

    c_fen: Fen
    e_fen: NonNegativeFen
    l_fen: NonNegativeFen
    b_fen: NonNegativeFen
    m_fen: NonNegativeFen
    u_fen: NonNegativeFen
    protected_fen: NonNegativeFen
    headroom_fen: NonNegativeFen
    shortfall_fen: NonNegativeFen
    bills: tuple[BillState, ...]
    monthly_estimate_fen: Fen
    quality: SnapshotQuality
    inputs_hash: Sha256Hex


# Funds engine (SPEC §14)


class Lot(ContractModel):
    lot_id: LotId
    product_id: FundCode
    shares: Shares
    acquired_on: date
    cost_fen: NonNegativeFen


class FeeTier(ContractModel):
    """Rate for lots held between min_days and max_days whole days, inclusive; None is open."""

    min_days: NonNegativeInt
    max_days: NonNegativeInt | None
    rate: Rate


class FeeSchedule(ContractModel):
    schedule_id: FeeScheduleId
    terms_version: str
    purchase_rate: Rate
    tiers: tuple[FeeTier, ...]


class LotDraw(ContractModel):
    """Shares taken from one lot by a redemption (FIFO in the simulator)."""

    lot_id: LotId
    shares: Shares
    held_days: NonNegativeInt
    rate: Rate


class FeeStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


class FeeQuote(ContractModel):
    """Gross, fee and net of a redemption. Unknown lots give `unverified` and no fee, never 0."""

    shares: Shares
    gross_fen: NonNegativeFen
    fee_fen: NonNegativeFen | None
    net_fen: NonNegativeFen | None
    status: FeeStatus
    terms_version: str
    draws: tuple[LotDraw, ...]

    @model_validator(mode="after")
    def _fee_known_when_verified(self) -> FeeQuote:
        known = self.fee_fen is not None and self.net_fen is not None
        if known != (self.status is FeeStatus.VERIFIED):
            raise ValueError("fee_fen and net_fen are set exactly when the quote is verified")
        return self


class HoldingRow(ContractModel):
    security_code: str
    issuer: str
    sector: str
    weight: Weight


class HoldingsSnapshot(ContractModel):
    product_id: FundCode
    as_of: date
    source_id: str
    executable: StrictBool
    taxonomy_version: str
    unresolved_weight: Weight
    rows: tuple[HoldingRow, ...]


class PositionValue(ContractModel):
    product_id: FundCode
    shares: Shares
    nav: Nav
    value_fen: NonNegativeFen


class ExposureGroup(StrEnum):
    SECTOR = "sector"
    ISSUER = "issuer"


class TargetStatus(StrEnum):
    """`met`: upper ≤ target; `not_met`: lower > target; `unverified`: lower ≤ target < upper."""

    MET = "met"
    NOT_MET = "not_met"
    UNVERIFIED = "unverified"


class ExposureResult(ContractModel):
    """Exposure of one sector or issuer: lower = Σ V·d / Σ V, upper = Σ V·(d + u) / Σ V."""

    group: ExposureGroup
    key: str
    total_fen: NonNegativeFen
    lower_fen: NonNegativeFen
    upper_fen: NonNegativeFen
    lower_pct: Pct
    upper_pct: Pct
    target_max_pct: Pct | None
    status: TargetStatus | None
    holdings_as_of: date
    taxonomy_version: str
    unresolved_pct: Pct
    executable: StrictBool

    @model_validator(mode="after")
    def _status_needs_target(self) -> ExposureResult:
        if (self.status is None) != (self.target_max_pct is None):
            raise ValueError("status is set exactly when a target is given")
        return self


class FundProduct(ContractModel):
    product_id: FundCode
    nav: Nav
    min_purchase_fen: PositiveFen
    redeem_settle_days: NonNegativeInt
    purchase_settle_days: NonNegativeInt
    fee_schedule: FeeSchedule


class CandidateKind(StrEnum):
    SWAP = "swap"
    NO_TRADE = "no_trade"
    SELL_ONLY = "sell_only"
    BUY_ONLY = "buy_only"


class CandidateReason(StrEnum):
    """Why a candidate fails; values equal the error code where one exists."""

    TARGET_NOT_MET = "TARGET_NOT_MET"
    TARGET_UNVERIFIED = "TARGET_UNVERIFIED"
    FEE_CAP = "FEE_CAP"
    FEE_UNVERIFIED = "FEE_UNVERIFIED"
    TURNOVER_CAP = "TURNOVER_CAP"
    PURCHASE_BELOW_MIN = "PURCHASE_BELOW_MIN"
    SHARE_ROUNDING = "SHARE_ROUNDING"
    INSUFFICIENT_HEADROOM = "INSUFFICIENT_HEADROOM"
    SETTLEMENT_CALENDAR = "SETTLEMENT_CALENDAR"


class Candidate(ContractModel):
    kind: CandidateKind
    sell_product: FundCode | None
    buy_product: FundCode | None
    gross_fen: NonNegativeFen
    fee_fen: NonNegativeFen | None
    purchase_fen: NonNegativeFen
    shares_redeemed: Shares
    turnover_fen: NonNegativeFen
    checkpoint: ExposureResult
    passes: StrictBool
    reasons: tuple[CandidateReason, ...]

    @model_validator(mode="after")
    def _passes_without_reasons(self) -> Candidate:
        if self.passes == bool(self.reasons):
            raise ValueError("a candidate passes exactly when it has no failure reasons")
        return self


class CandidateSearchInput(ContractModel):
    """Everything the grid search needs; purchase must fit headroom after the redemption settles."""

    as_of: date
    checkpoint_on: date
    positions: tuple[PositionValue, ...]
    snapshots: tuple[HoldingsSnapshot, ...]
    products: tuple[FundProduct, ...]
    lots: tuple[Lot, ...]
    sector: str
    target_max_pct: Pct
    fee_cap_fen: NonNegativeFen
    turnover_cap_fen: NonNegativeFen
    headroom_fen: NonNegativeFen
    cash_snapshot_id: CashSnapshotId
    grid_fen: tuple[PositiveFen, ...] = (100000, 200000, 300000, 400000, 500000)


class CandidateSearchResult(ContractModel):
    """All candidates ranked; `selected` is the smallest passing swap in the grid, if any."""

    start: ExposureResult
    candidates: tuple[Candidate, ...]
    selected: Candidate | None
    cash_snapshot_id: CashSnapshotId


class StressShock(ContractModel):
    """A fixed shock: percent loss per fund ("20" is a 20 % fall)."""

    name: str
    loss_pct: dict[FundCode, Pct]


class StressRow(ContractModel):
    shock: str
    before_loss_fen: NonNegativeFen
    after_loss_fen: NonNegativeFen
    change_fen: Fen


class StressResult(ContractModel):
    """Losses before and after the trade; the after loss includes the trade fee."""

    fee_fen: NonNegativeFen
    rows: tuple[StressRow, ...]


class PlanBounds(ContractModel):
    p0_fen: NonNegativeFen
    spare0_fen: NonNegativeFen
    max_added_fen: NonNegativeFen
    max_retained_fen: NonNegativeFen
    min_purchase_fen: NonNegativeFen


class RevisionOutcome(StrEnum):
    UNCHANGED = "unchanged"
    REVISE = "revise"
    PAUSE = "pause"


class RevisionDecision(ContractModel):
    """Result of the C-09 rule; purchase_fen is P(x), also reported when the purchase pauses."""

    outcome: RevisionOutcome
    x_fen: NonNegativeFen
    purchase_fen: Fen
    reasons: tuple[ErrorCode, ...]

    @model_validator(mode="after")
    def _reasons_only_on_pause(self) -> RevisionDecision:
        if (self.outcome is RevisionOutcome.PAUSE) != bool(self.reasons):
            raise ValueError("a decision has reasons exactly when it pauses")
        return self


# Signatures implemented in wg.numeric


class ComputeSnapshot(Protocol):
    def __call__(self, inputs: CashInputs) -> CashSnapshotValues: ...


class Exposure(Protocol):
    def __call__(
        self,
        positions: Sequence[PositionValue],
        snapshots: Sequence[HoldingsSnapshot],
        group: ExposureGroup,
        key: str,
        target_max_pct: Decimal | None,
    ) -> ExposureResult: ...


class RedemptionFee(Protocol):
    def __call__(
        self,
        lots: Sequence[Lot],
        shares: Decimal,
        nav: Decimal,
        schedule: FeeSchedule,
        as_of: date,
    ) -> FeeQuote: ...


class SearchCandidates(Protocol):
    def __call__(self, inputs: CandidateSearchInput) -> CandidateSearchResult: ...


class Stress(Protocol):
    def __call__(
        self,
        shocks: Sequence[StressShock],
        before: Sequence[PositionValue],
        after: Sequence[PositionValue],
        fee_fen: int,
    ) -> StressResult: ...


class DecidePurchase(Protocol):
    def __call__(
        self, x_fen: int, bounds: PlanBounds | None, current_fen: int, headroom_fen: int
    ) -> RevisionDecision: ...
