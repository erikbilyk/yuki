"""K5 plan validator (SPEC §15.1): a composed plan is rejected before any card is shown.

The validator sees the proposed nodes and the money facts at composition time; it reads no
database. Codes come in a fixed order and each appears once. A plan has no conditional
branches: a fallback is a new draft with its own approval.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from wg.contracts.enums import ActionKind, CommitmentStatus
from wg.contracts.errors import ErrorCode
from wg.contracts.plan import PlanValidationResult

COMPOSABLE_KINDS = frozenset(
    {
        ActionKind.TRANSFER,
        ActionKind.PAY_BILL,
        ActionKind.DESIGNATE_RESERVE,
        ActionKind.REDEEM,
        ActionKind.PURCHASE,
        ActionKind.CHECK_OUTCOME,
    }
)
PAYABLE = frozenset({CommitmentStatus.CONFIRMED, CommitmentStatus.PARTIALLY_PAID})

CODE_ORDER = (
    ErrorCode.NOT_IN_PLAN,
    ErrorCode.UNKNOWN_ACTION_KIND,
    ErrorCode.PLAN_CYCLE,
    ErrorCode.UNFUNDED_DEPENDENCY,
    ErrorCode.BILL_NOT_PAYABLE,
    ErrorCode.PAYEE_MISMATCH,
    ErrorCode.DUPLICATE_INTENT,
    ErrorCode.NOT_BEFORE_AFTER_EXPIRY,
    ErrorCode.PLAN_UNFUNDED,
)


@dataclass(frozen=True)
class ProposedNode:
    logical_action_id: str
    kind: ActionKind
    params: Mapping[str, Any]
    depends_on: tuple[str, ...] = ()
    not_before: datetime | None = None


@dataclass(frozen=True)
class Bill:
    status: CommitmentStatus
    payee_id: str


@dataclass(frozen=True)
class Proceeds:
    """When a redemption's money arrives and how much; None when its fee is unknown."""

    settles_at: datetime
    net_fen: int | None


@dataclass(frozen=True)
class MoneyFacts:
    """What the customer has when the plan is composed (posted money, current headroom)."""

    now: datetime
    expires_at: datetime
    available_fen: Mapping[str, int]
    covered_accounts: frozenset[str]
    headroom_fen: int
    reserve_fen: int
    bills: Mapping[str, Bill] = field(default_factory=dict)
    book: frozenset[str] = frozenset()
    proceeds: Mapping[str, Proceeds] = field(default_factory=dict)


def validate_plan(nodes: Sequence[ProposedNode], facts: MoneyFacts) -> PlanValidationResult:
    """All reasons the plan cannot be shown for approval, in CODE_ORDER."""
    found: set[ErrorCode] = set()
    ids = {node.logical_action_id for node in nodes}
    if any(d not in ids for node in nodes for d in node.depends_on):
        found.add(ErrorCode.NOT_IN_PLAN)
    if any(node.kind not in COMPOSABLE_KINDS for node in nodes):
        found.add(ErrorCode.UNKNOWN_ACTION_KIND)
    order = topological_order(nodes)
    if order is None:
        found.add(ErrorCode.PLAN_CYCLE)
    found.update(_funding_dependencies(nodes))
    found.update(_bill_codes(nodes, facts))
    if _duplicate_intents(nodes):
        found.add(ErrorCode.DUPLICATE_INTENT)
    if any(n.not_before is not None and n.not_before >= facts.expires_at for n in nodes):
        found.add(ErrorCode.NOT_BEFORE_AFTER_EXPIRY)
    if order is not None and not found & {ErrorCode.NOT_IN_PLAN, ErrorCode.UNKNOWN_ACTION_KIND}:
        if not _funded(order, facts):
            found.add(ErrorCode.PLAN_UNFUNDED)
    codes = tuple(code for code in CODE_ORDER if code in found)
    return PlanValidationResult(ok=not codes, codes=codes)


def topological_order(nodes: Sequence[ProposedNode]) -> list[ProposedNode] | None:
    """The nodes with every dependency before its dependants, stable; None on a cycle."""
    by_id = {node.logical_action_id: node for node in nodes}
    placed: list[ProposedNode] = []
    done: set[str] = set()
    remaining = list(nodes)
    while remaining:
        ready = [n for n in remaining if all(d in done or d not in by_id for d in n.depends_on)]
        if not ready:
            return None
        for node in ready:
            placed.append(node)
            done.add(node.logical_action_id)
        remaining = [n for n in remaining if n.logical_action_id not in done]
    return placed


def _funding_dependencies(nodes: Sequence[ProposedNode]) -> set[ErrorCode]:
    """A purchase is paid from settled proceeds, so it waits for every redemption of the plan."""
    redemptions = {n.logical_action_id for n in nodes if n.kind is ActionKind.REDEEM}
    for node in nodes:
        if node.kind is ActionKind.PURCHASE and not redemptions <= set(node.depends_on):
            return {ErrorCode.UNFUNDED_DEPENDENCY}
    return set()


def _bill_codes(nodes: Sequence[ProposedNode], facts: MoneyFacts) -> set[ErrorCode]:
    codes: set[ErrorCode] = set()
    for node in nodes:
        if node.kind is not ActionKind.PAY_BILL:
            continue
        bill = facts.bills.get(str(node.params.get("commitment_id")))
        if bill is None or bill.status not in PAYABLE:
            codes.add(ErrorCode.BILL_NOT_PAYABLE)
            continue
        payee_id = node.params.get("payee_id")
        if payee_id != bill.payee_id or payee_id not in facts.book:
            codes.add(ErrorCode.PAYEE_MISMATCH)
    return codes


def _intent(node: ProposedNode) -> tuple[Any, ...]:
    """What one node is for; two active nodes of the plan may not share it."""
    params = node.params
    if node.kind is ActionKind.PAY_BILL:
        return (node.kind, params.get("commitment_id"))
    if node.kind in (ActionKind.REDEEM, ActionKind.PURCHASE):
        return (node.kind, params.get("product_id"))
    if node.kind is ActionKind.TRANSFER:
        return (node.kind, params.get("payee_id"), params.get("amount_fen"))
    return (node.kind,)


def _duplicate_intents(nodes: Sequence[ProposedNode]) -> bool:
    intents = [_intent(node) for node in nodes]
    return len(set(intents)) != len(intents)


def _funded(order: Sequence[ProposedNode], facts: MoneyFacts) -> bool:
    """Walk the plan's money in time order: every debit fits at its date (SPEC §15.1).

    A bill payment is funded by its own protection, so it needs posted money only; a
    transfer or a purchase needs money and headroom. A redemption adds its net proceeds
    when it settles, never before. Raising the reserve takes headroom at once.
    """
    cash = dict(facts.available_fen)
    headroom = facts.headroom_fen
    done_at: dict[str, datetime] = {}
    steps: list[tuple[datetime, int, int, ProposedNode]] = []
    for index, node in enumerate(order):
        waits = [done_at.get(d, facts.now) for d in node.depends_on]
        start = max([node.not_before or facts.now, *waits])
        if node.kind is ActionKind.REDEEM:
            proceeds = facts.proceeds.get(node.logical_action_id)
            done_at[node.logical_action_id] = max(start, proceeds.settles_at) if proceeds else start
            steps.append((done_at[node.logical_action_id], 0, index, node))
        else:
            done_at[node.logical_action_id] = start
            steps.append((start, 1, index, node))

    for _, _, _, node in sorted(steps, key=lambda step: step[:3]):
        params = node.params
        account = str(params.get("account_id"))
        if node.kind is ActionKind.REDEEM:
            proceeds = facts.proceeds.get(node.logical_action_id)
            net = proceeds.net_fen if proceeds and proceeds.net_fen is not None else 0
            cash[account] = cash.get(account, 0) + net
            if account in facts.covered_accounts:
                headroom += net
        elif node.kind is ActionKind.DESIGNATE_RESERVE:
            headroom -= int(params["target_fen"]) - facts.reserve_fen
        elif node.kind in (ActionKind.PAY_BILL, ActionKind.TRANSFER, ActionKind.PURCHASE):
            amount = int(params["amount_fen"])
            cash[account] = cash.get(account, 0) - amount
            if cash[account] < 0:
                return False
            if node.kind is not ActionKind.PAY_BILL and account in facts.covered_accounts:
                headroom -= amount
                if headroom < 0:
                    return False
    return True
