"""Typed facts behind every reply and the result of checking an explanation (SPEC §17)."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, NonNegativeInt, computed_field, model_validator

from wg.contracts._base import ContractModel
from wg.contracts.money import Fen, Pct


class FactKind(StrEnum):
    MONEY = "money"
    PERCENT = "percent"
    STATUS = "status"
    NAME = "name"
    DATE = "date"
    COUNT = "count"


class FactConcept(StrEnum):
    """What a fact means; a claim must match a fact of the right concept, not only its value."""

    POSTED_CASH = "posted_cash"
    ACCOUNT_BALANCE = "account_balance"
    HEADROOM = "headroom"
    PROTECTED = "protected"
    SHORTFALL = "shortfall"
    MONTHLY_ESTIMATE = "monthly_estimate"
    RESERVE_TARGET = "reserve_target"
    ALLOWANCE_REMAINING = "allowance_remaining"
    BILL_AMOUNT = "bill_amount"
    BILL_UNPAID = "bill_unpaid"
    BILL_STATUS = "bill_status"
    OPERATION_AMOUNT = "operation_amount"
    RECEIPT_STATUS = "receipt_status"
    ACTION_STATUS = "action_status"
    FEE = "fee"
    REDEMPTION_GROSS = "redemption_gross"
    REDEMPTION_NET = "redemption_net"
    PURCHASE_AMOUNT = "purchase_amount"
    TURNOVER = "turnover"
    EXPOSURE_VALUE = "exposure_value"
    SECTOR_SHARE = "sector_share"
    ISSUER_SHARE = "issuer_share"
    FUND_WEIGHT = "fund_weight"
    TARGET_MAX = "target_max"
    STRESS_LOSS = "stress_loss"
    CHECKPOINT_RESULT = "checkpoint_result"
    PLAN_STATUS = "plan_status"
    OPTION_FEASIBILITY = "option_feasibility"
    UNRESOLVED_COUNT = "unresolved_count"
    PAYEE = "payee"
    PRODUCT = "product"
    ACCOUNT = "account"
    DUE_DATE = "due_date"
    SETTLE_DATE = "settle_date"


class _FactBase(ContractModel):
    fact_id: Annotated[str, Field(min_length=1)]
    concept: FactConcept
    subject: str | None = None
    source_ref: Annotated[str, Field(min_length=1)]
    as_of: AwareDatetime
    label: str


class MoneyFact(_FactBase):
    kind: Literal[FactKind.MONEY] = FactKind.MONEY
    value: Fen
    unit: Literal["fen"] = "fen"


class PercentFact(_FactBase):
    kind: Literal[FactKind.PERCENT] = FactKind.PERCENT
    value: Pct
    unit: Literal["percent"] = "percent"


class CountFact(_FactBase):
    kind: Literal[FactKind.COUNT] = FactKind.COUNT
    value: Annotated[int, Field(strict=True, ge=0)]
    unit: Literal["count"] = "count"


class StatusFact(_FactBase):
    kind: Literal[FactKind.STATUS] = FactKind.STATUS
    value: str
    unit: None = None


class NameFact(_FactBase):
    kind: Literal[FactKind.NAME] = FactKind.NAME
    value: str
    unit: None = None


class DateFact(_FactBase):
    kind: Literal[FactKind.DATE] = FactKind.DATE
    value: date
    unit: None = None


Fact = Annotated[
    MoneyFact | PercentFact | CountFact | StatusFact | NameFact | DateFact,
    Field(discriminator="kind"),
]


class FactBlock(ContractModel):
    facts: tuple[Fact, ...]

    @model_validator(mode="after")
    def _unique_fact_ids(self) -> FactBlock:
        ids = [fact.fact_id for fact in self.facts]
        if len(ids) != len(set(ids)):
            raise ValueError("fact_id values must be unique within a block")
        return self


class ClaimViolationReason(StrEnum):
    UNSUPPORTED = "unsupported"
    VALUE_MISMATCH = "value_mismatch"
    CONCEPT_MISMATCH = "concept_mismatch"
    PRECISION = "precision"
    STATUS_UNSUPPORTED = "status_unsupported"
    FEASIBILITY_DENIED = "feasibility_denied"


class ClaimViolation(ContractModel):
    """One claim in the explanation that no fact supports; start and end index the text."""

    claim_kind: FactKind
    text: str
    start: NonNegativeInt
    end: NonNegativeInt
    reason: ClaimViolationReason
    fact_id: str | None = None


class ClaimCheckResult(ContractModel):
    claims_checked: NonNegativeInt
    violations: tuple[ClaimViolation, ...]

    @computed_field
    @property
    def passed(self) -> bool:
        return not self.violations
