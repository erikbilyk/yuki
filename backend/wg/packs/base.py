"""The pack protocol: what each action kind contributes to drafting and execution (SPEC §12.1).

The gateway owns the protocol of every execution (idempotency, authority, the operation
row, events, receipts). A pack supplies only what differs by kind: canonical params and
their hash, the tier, the card, the prechecks, the operation's terms, the reservation and
the bank calls.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from pydantic import JsonValue

from wg.contracts.enums import ActionKind, Tier
from wg.contracts.errors import ErrorCode, WgError
from wg.core.k4_approval import CardContent
from wg.k6.actions import ActionRow
from wg.k6.bank import BankRequest
from wg.k6.db import Reader, UnitOfWork
from wg.k6.operations import OperationRow, ReservationRow


@dataclass(frozen=True)
class Infeasible:
    """One reason an action cannot execute now, with the facts behind it."""

    code: ErrorCode
    detail: dict[str, JsonValue] = field(default_factory=dict)

    def error(self) -> WgError:
        return WgError(self.code, self.detail)


@dataclass(frozen=True)
class OperationTerms:
    """The fields of the operation row and its reservation for one action."""

    account_id: str
    settle_due_at: datetime
    settles_at_once: bool
    amount_fen: int | None = None
    shares: Decimal | None = None
    product_id: str | None = None
    payee_id: str | None = None
    commitment_id: str | None = None
    purpose_id: str | None = None


class Pack(Protocol):
    """What every action kind contributes before execution: params, hash, tier, checks, card."""

    kinds: frozenset[ActionKind]

    def canonical(self, kind: ActionKind, params: Mapping[str, Any]) -> dict[str, Any]:
        """Params in their stored form; raises ValueError for a malformed set."""
        ...

    def request_hash(self, kind: ActionKind, customer_id: str, params: Mapping[str, Any]) -> str:
        """SHA-256 of the business fields only (SPEC §12.2)."""
        ...

    def tier(
        self,
        reader: Reader,
        customer_id: str,
        kind: ActionKind,
        params: Mapping[str, Any],
        at: datetime,
    ) -> Tier: ...

    def precheck(
        self,
        reader: Reader,
        customer_id: str,
        kind: ActionKind,
        params: Mapping[str, Any],
        at: datetime,
        *,
        plan_id: str | None = None,
    ) -> tuple[Infeasible, ...]:
        """Every reason the action cannot execute at `at`, from current rows; empty if it can.

        `plan_id` is the plan the action runs in, for checks that span the plan's actions.
        """
        ...

    def card(self, reader: Reader, action: ActionRow, at: datetime) -> CardContent: ...


class ActionPack(Pack, Protocol):
    """A pack whose actions move money through the bank simulator and leave an operation."""

    def operation_terms(
        self, reader: Reader, action: ActionRow, at: datetime
    ) -> OperationTerms: ...

    def reserve(
        self, uow: UnitOfWork, operation: OperationRow, terms: OperationTerms
    ) -> ReservationRow | None:
        """Hold the money the operation will debit; an order that debits nothing holds none."""
        ...

    def apply(
        self, uow: UnitOfWork, operation: OperationRow, terms: OperationTerms, at: datetime
    ) -> BankRequest:
        """The bank call; raises AckLost when the acknowledgement is lost."""
        ...

    def settle(self, uow: UnitOfWork, operation: OperationRow, at: datetime) -> BankRequest: ...

    def on_settled(self, uow: UnitOfWork, operation: OperationRow) -> dict[str, Any]:
        """Effects of a settlement beyond the ledger; the result is merged into the receipt."""
        ...

    def receipt_effect(self, operation: OperationRow, bank: BankRequest) -> dict[str, Any]: ...


@runtime_checkable
class StatePack(Pack, Protocol):
    """A pack whose actions change the customer's own records and move no money.

    The gateway runs `execute` in the submitting unit of work; there is no operation, bank
    call or receipt.
    """

    def execute(self, uow: UnitOfWork, action: ActionRow, at: datetime) -> None: ...


def fen_text(fen: int) -> str:
    """¥1,234.56 for 123456 fen."""
    sign = "-" if fen < 0 else ""
    yuan, cents = divmod(abs(fen), 100)
    return f"{sign}¥{yuan:,}.{cents:02d}"
