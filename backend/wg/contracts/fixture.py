"""Model of a fixture profile file (SPEC §23.2.1) and its loader with named variants.

Variant paths are dotted; a segment selects a mapping key or the list element whose
`id` (for transactions `source_txn_id`) equals it. `set` replaces an existing value,
`remove` deletes list elements by id. Loading never touches a database.
"""

from __future__ import annotations

import calendar
import copy
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import (
    AwareDatetime,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from wg.contracts._base import ContractModel
from wg.contracts.engines import FeeTier, HoldingRow, PlanBounds
from wg.contracts.enums import CommitmentStatus, MandateStatus, PayeeBank, Recurrence, Role
from wg.contracts.ids import (
    AccountId,
    CommitmentId,
    CustomerId,
    FeeScheduleId,
    FundCode,
    LotId,
    MandateRef,
    MerchantId,
    OperatorId,
    PayeeId,
    PurposeId,
    StatementId,
)
from wg.contracts.money import Fen, Nav, NonNegativeFen, Pct, PositiveFen, Rate, Shares, Weight

_ELEMENT_KEYS = ("id", "source_txn_id")
# Day offsets count from the epoch's calendar date in Asia/Shanghai, as the loader does.
_SHANGHAI = ZoneInfo("Asia/Shanghai")

NonNegativeDays = Annotated[int, Field(strict=True, ge=0)]
DebitDay = Annotated[int, Field(strict=True, ge=1, le=31)]
Period = Annotated[
    str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9]{4}-[0-9]{2}-[0-9]{2}$")
]
Month = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}$")]
MaskedAccount = Annotated[str, StringConstraints(pattern=r"^··[0-9]{4}$")]


class FixtureError(ValueError):
    """The fixture file or a variant overlay is malformed."""


class FixtureCustomer(ContractModel):
    id: CustomerId
    handle: str
    password: str
    display_name: str


class FixtureOperator(ContractModel):
    id: OperatorId
    handle: str
    password: str
    role: Literal[Role.OPERATOR, Role.DEMO]


class FixtureAccount(ContractModel):
    id: AccountId
    customer: CustomerId
    name: str
    covered: StrictBool
    opening_fen: NonNegativeFen


class FixturePayee(ContractModel):
    id: PayeeId
    customer: CustomerId
    alias: str
    display_name: str
    bank: PayeeBank
    account_masked: MaskedAccount
    account_ref: str


class FixtureTransaction(ContractModel):
    """A statement line; amounts are signed (credit positive), text fields are untrusted."""

    source_txn_id: str
    posted_offset_days: StrictInt
    amount_fen: Fen
    counterparty: str
    description_raw: str
    category_hint: str | None


class FixtureStatement(ContractModel):
    id: StatementId
    account: AccountId
    period: Period
    opening_fen: Fen
    closing_fen: Fen
    transactions: tuple[FixtureTransaction, ...]


class FixtureCommitment(ContractModel):
    id: CommitmentId
    customer: CustomerId
    purpose_id: PurposeId
    kind: str
    payee: PayeeId
    amount_fen: PositiveFen
    unpaid_fen: NonNegativeFen
    due_offset_days: StrictInt
    recurrence: Recurrence
    source: str
    status: CommitmentStatus


class FixtureReserve(ContractModel):
    target_fen: NonNegativeFen


class FixtureAllowance(ContractModel):
    period: Month
    budget_fen: NonNegativeFen
    remaining_fen: NonNegativeFen
    excluded_purpose_ids: tuple[PurposeId, ...]


class FixtureProduct(ContractModel):
    id: FundCode
    name: str
    share_class: str
    nav: Nav
    nav_date_offset_days: StrictInt
    fee_schedule: FeeScheduleId
    min_purchase_fen: PositiveFen
    redeem_settle_days: NonNegativeDays
    purchase_settle_days: NonNegativeDays


class FixtureFeeSchedule(ContractModel):
    id: FeeScheduleId
    terms_version: str
    purchase_rate: Rate
    tiers: tuple[FeeTier, ...]


class FixturePosition(ContractModel):
    account: AccountId
    product: FundCode
    shares: Shares


class FixtureLot(ContractModel):
    id: LotId
    product: FundCode
    shares: Shares
    acquired_offset_days: StrictInt
    cost_fen: NonNegativeFen


class FixtureHoldingsSnapshot(ContractModel):
    product: FundCode
    as_of: date
    source_id: str
    executable: StrictBool
    taxonomy_version: str
    unresolved_weight: Weight
    rows: tuple[HoldingRow, ...]


class FixturePlanConstants(ContractModel):
    sector: str
    target_max_pct: Pct
    checkpoint_day: NonNegativeDays
    fee_cap_fen: NonNegativeFen
    turnover_cap_fen: NonNegativeFen
    expiry_day: NonNegativeDays
    reserve_after_plan_fen: NonNegativeFen
    bounds: PlanBounds


class FixtureMerchant(ContractModel):
    """A direct-debit merchant; only verified merchants are in the registry."""

    id: MerchantId
    display_name: str
    aliases: tuple[str, ...]
    settlement_account_ref: str
    verified: StrictBool


class FixtureMandate(ContractModel):
    """A direct-debit agreement; its term is given as day offsets from the epoch."""

    id: MandateRef
    customer: CustomerId
    account: AccountId
    merchant: MerchantId
    purpose_id: PurposeId
    expected_fen: PositiveFen
    cap_fen: PositiveFen
    debit_day: DebitDay
    starts_offset_days: StrictInt
    expires_offset_days: StrictInt
    status: Literal[MandateStatus.ACTIVE, MandateStatus.SUSPENDED]


def plus_months(day: date, months: int) -> date:
    """`day` moved by whole calendar months, overflowing into the next month as SQLite does.

    SQLite's `date(d, '+12 months')` turns 2028-02-29 into 2029-03-01; the schema's term
    check uses it, so the fixture check must agree.
    """
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    last = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(day.day, last)) + timedelta(days=max(0, day.day - last))


class FixtureVariant(ContractModel):
    set_values: dict[str, Any] = Field(default_factory=dict, alias="set")
    remove: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class Fixture(ContractModel):
    profile: str
    epoch: AwareDatetime
    customers: tuple[FixtureCustomer, ...]
    operators: tuple[FixtureOperator, ...]
    accounts: tuple[FixtureAccount, ...]
    payees: tuple[FixturePayee, ...]
    statements: tuple[FixtureStatement, ...]
    commitments: tuple[FixtureCommitment, ...]
    reserve: FixtureReserve
    allowance: FixtureAllowance
    margin_fen: NonNegativeFen
    income_this_month_fen: NonNegativeFen
    spent_this_month_fen: NonNegativeFen
    products: tuple[FixtureProduct, ...]
    fee_schedules: tuple[FixtureFeeSchedule, ...]
    positions: tuple[FixturePosition, ...]
    lots: tuple[FixtureLot, ...]
    holdings_snapshots: tuple[FixtureHoldingsSnapshot, ...]
    plan_constants: FixturePlanConstants
    merchants: tuple[FixtureMerchant, ...] = ()
    mandates: tuple[FixtureMandate, ...] = ()
    variants: dict[str, FixtureVariant] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_ids(self) -> Fixture:
        groups: dict[str, list[str]] = {
            name: [item.id for item in getattr(self, name)]
            for name in (
                "customers",
                "operators",
                "accounts",
                "payees",
                "statements",
                "commitments",
                "products",
                "fee_schedules",
                "lots",
                "merchants",
                "mandates",
            )
        }
        groups["transactions"] = [
            f"{statement.account}:{txn.source_txn_id}"
            for statement in self.statements
            for txn in statement.transactions
        ]
        for name, ids in groups.items():
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate id in {name}")
        return self

    @model_validator(mode="after")
    def _mandate_terms(self) -> Fixture:
        epoch_date = self.epoch.astimezone(_SHANGHAI).date()
        for mandate in self.mandates:
            starts = epoch_date + timedelta(days=mandate.starts_offset_days)
            expires = epoch_date + timedelta(days=mandate.expires_offset_days)
            if not starts <= expires <= plus_months(starts, 12):
                raise ValueError(f"mandate {mandate.id}: term must run 0 to 12 months")
        return self


def _element_key(element: Any) -> str | None:
    if isinstance(element, dict):
        for key in _ELEMENT_KEYS:
            if key in element:
                return key
    return None


def _step(node: Any, segment: str, path: str) -> Any:
    if isinstance(node, dict):
        if segment in node:
            return node[segment]
    elif isinstance(node, list):
        for element in node:
            key = _element_key(element)
            if key is not None and element[key] == segment:
                return element
    raise FixtureError(f"variant path {path!r}: no element {segment!r}")


def _apply_variant(profile: dict[str, Any], name: str, variant: FixtureVariant) -> None:
    for path, value in variant.set_values.items():
        *parents, leaf = path.split(".")
        node: Any = profile
        for segment in parents:
            node = _step(node, segment, path)
        if not isinstance(node, dict) or leaf not in node:
            raise FixtureError(f"variant {name!r}: path {path!r} does not exist")
        node[leaf] = copy.deepcopy(value)
    for path, ids in variant.remove.items():
        node = profile
        for segment in path.split("."):
            node = _step(node, segment, path)
        if not isinstance(node, list):
            raise FixtureError(f"variant {name!r}: {path!r} is not a list")
        kept = [item for item in node if item.get(_element_key(item) or "") not in ids]
        if len(node) - len(kept) != len(set(ids)):
            raise FixtureError(f"variant {name!r}: unknown id in {path!r}")
        node[:] = kept


def load_fixture(path: Path, variant: str | None = None) -> Fixture:
    """Read and validate a fixture profile, with the named variant applied if given."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise FixtureError(f"{path}: not valid YAML") from exc
    if not isinstance(raw, dict):
        raise FixtureError(f"{path}: top level must be a mapping")
    try:
        if variant is not None:
            overlays = raw.get("variants") or {}
            if variant not in overlays:
                raise FixtureError(f"{path}: unknown variant {variant!r}")
            profile = copy.deepcopy(raw)
            _apply_variant(profile, variant, FixtureVariant.model_validate(overlays[variant]))
            raw = profile
        return Fixture.model_validate(raw)
    except ValidationError as exc:
        raise FixtureError(f"{path}: {exc}") from exc
