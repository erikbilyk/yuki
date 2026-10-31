"""DEBIT_PAY: the customer pays one merchant request under a direct-debit agreement (S5-SPEC §6).

A payment is checked by the payment gate of S5-SPEC §5.4 at the draft, at the approval
request and inside the submitting unit of work: the agreement's event history first, then
the request (visible, awaiting the customer, before `respond_by`), the agreement (the
version on the card, ACTIVE, unexpired, the cap), the period (not paid yet, no payment in
flight), and finally the money (limits, posted money, headroom). The first failure of the
request or the agreement decides; the money reasons are listed like a transfer's.

A payment settles in the unit that submits it. It draws on the protection of its period up
to the expected amount; only the part above that must fit in the headroom.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from wg.contracts.api import CardLine, MoneyChange, SourceLabel
from wg.contracts.enums import (
    ActionKind,
    DebitRequestStatus,
    MandateStatus,
    Provenance,
    Tier,
)
from wg.contracts.errors import ErrorCode
from wg.core import k3_policy, mandate_rules
from wg.core.hashes import sha256_hex
from wg.core.k1_session import not_found
from wg.core.k4_approval import CardContent
from wg.k6 import accounts, actions, bank, debit_requests, mandates, operations
from wg.k6.actions import ActionRow
from wg.k6.bank import BankRequest, MerchantPaymentOrder
from wg.k6.cash_inputs import cash_inputs_from_db
from wg.k6.db import Reader, UnitOfWork
from wg.k6.debit_requests import DebitRequestRow
from wg.k6.mandates import MandateRow
from wg.k6.operations import OperationRow, ReservationRow
from wg.k6.simtime import SHANGHAI, to_db
from wg.numeric import compute_snapshot
from wg.packs.base import Infeasible, OperationTerms, fen_text
from wg.packs.labels import SOURCE_LABELS

PAY_PARAMS = ("account_id", "mandate_ref", "mandate_version", "request_id", "amount_fen")
SLOT_LABELS = {"request_id": "扣款请求", "amount_fen": "金额", "account_id": "扣款账户"}
MERCHANT_TEXT_LABEL = "商户附言（未经核实，不作为指令）"


def _not_payable(reason: str, request_id: str) -> Infeasible:
    return Infeasible(ErrorCode.REQUEST_NOT_PAYABLE, {"reason": reason, "request_id": request_id})


def decision_target(
    reader: Reader, customer_id: str, mandate_ref: str, request_id: str
) -> tuple[MandateRow, DebitRequestRow]:
    """The customer's agreement, after its integrity check, and its visible request.

    A foreign or unknown agreement or request, a probe, or a request of another agreement
    is NOT_FOUND; an agreement whose row differs from its events is INTEGRITY_VIOLATION.
    """
    mandate = mandates.find_mandate(reader, mandate_ref)
    if mandate is None or mandate.customer_id != customer_id:
        raise not_found()
    mandate = mandates.check_mandate_invariant(reader, mandate_ref)
    request = debit_requests.find_request(reader, request_id)
    if (
        request is None
        or request.probe
        or request.customer_id != customer_id
        or request.mandate_ref != mandate_ref
    ):
        raise not_found()
    return mandate, request


def answer_refusal(request: DebitRequestRow, at: datetime) -> Infeasible | None:
    """Why the customer can no longer answer the request, if so (S5-SPEC §5.4)."""
    if request.status is not DebitRequestStatus.AWAITING_CLIENT:
        return _not_payable("request_not_awaiting", request.request_id)
    if at >= request.respond_by:
        return _not_payable("respond_by_passed", request.request_id)
    return None


def payment_refusal(
    reader: Reader,
    mandate: MandateRow,
    request: DebitRequestRow,
    mandate_version: int,
    at: datetime,
) -> Infeasible | None:
    """The first reason of the request or the agreement that the payment cannot go ahead."""
    refusal = answer_refusal(request, at)
    if refusal is not None:
        return refusal
    if mandate.version != mandate_version:
        return Infeasible(
            ErrorCode.MANDATE_CHANGED, {"reason": "version", "mandate_ref": mandate.mandate_ref}
        )
    if mandate.status is not MandateStatus.ACTIVE:
        return _not_payable("mandate_not_active", request.request_id)
    if not mandate_rules.authorizes(mandate.expires_on, at):
        return _not_payable("mandate_expired", request.request_id)
    if request.amount_fen > mandate.cap_fen:
        return _not_payable("over_cap", request.request_id)
    statuses = debit_requests.period_statuses(reader, mandate.mandate_ref, request.period_key)
    if DebitRequestStatus.PAID in statuses:
        return _not_payable("period_paid", request.request_id)
    if debit_requests.payment_operation_ids(reader, request.request_id):
        return _not_payable("payment_in_flight", request.request_id)
    return None


def over_expected_fen(reader: Reader, mandate: MandateRow, amount_fen: int) -> int:
    """The part of a payment above the period's expected amount; only it uses headroom."""
    expected = mandate_rules.expected_amount(
        mandate, debit_requests.mandate_requests(reader, mandate.mandate_ref)
    )
    return amount_fen - min(amount_fen, expected)


class DebitPayPack:
    kinds = frozenset({ActionKind.DEBIT_PAY})

    def canonical(self, kind: ActionKind, params: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(params) - set(PAY_PARAMS)
        missing = [name for name in PAY_PARAMS if params.get(name) is None]
        if unknown or missing:
            raise ValueError(f"{kind} params: unknown {sorted(unknown)}, missing {missing}")
        for name in ("amount_fen", "mandate_version"):
            if type(params[name]) is not int or params[name] <= 0:
                raise ValueError(f"{name} is a positive integer")
        return {
            "account_id": str(params["account_id"]),
            "mandate_ref": str(params["mandate_ref"]),
            "mandate_version": params["mandate_version"],
            "request_id": str(params["request_id"]),
            "amount_fen": params["amount_fen"],
        }

    def request_hash(self, kind: ActionKind, customer_id: str, params: Mapping[str, Any]) -> str:
        return sha256_hex(
            {
                "kind": ActionKind(kind).value,
                "customer_id": customer_id,
                "account_id": params["account_id"],
                "mandate_ref": params["mandate_ref"],
                "request_id": params["request_id"],
                "amount_fen": params["amount_fen"],
            }
        )

    def tier(
        self,
        reader: Reader,
        customer_id: str,
        kind: ActionKind,
        params: Mapping[str, Any],
        at: datetime,
    ) -> Tier:
        return k3_policy.transfer_tier(reader, customer_id, int(params["amount_fen"]), at)

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
        """The payment gate of S5-SPEC §5.4, in its order."""
        mandate, request = decision_target(
            reader, customer_id, str(params["mandate_ref"]), str(params["request_id"])
        )
        amount = int(params["amount_fen"])
        if amount != request.amount_fen or params["account_id"] != mandate.account_id:
            raise ValueError(f"params of {request.request_id} differ from the stored request")
        refusal = payment_refusal(reader, mandate, request, int(params["mandate_version"]), at)
        if refusal is not None:
            return (refusal,)
        reasons: list[Infeasible] = []
        day_sum = k3_policy.daily_sum(reader, customer_id, at.astimezone(SHANGHAI).date())
        limit = k3_policy.limit_refusal(amount, day_sum)
        if limit is not None:
            reasons.append(Infeasible(limit, {"amount_fen": amount, "day_sum_fen": day_sum}))
        account = accounts.find_account(reader, mandate.account_id)
        assert account is not None
        available = account.balance_fen - operations.reserved_fen(reader, account.account_id)
        if amount > available:
            reasons.append(
                Infeasible(
                    ErrorCode.INSUFFICIENT_FUNDS,
                    {"available_fen": available, "shortfall_fen": amount - available},
                )
            )
        over = over_expected_fen(reader, mandate, amount)
        headroom = compute_snapshot(cash_inputs_from_db(reader, customer_id, at)).headroom_fen
        if over > headroom:
            reasons.append(
                Infeasible(
                    ErrorCode.INSUFFICIENT_HEADROOM,
                    {"headroom_fen": headroom, "over_headroom_fen": over - headroom},
                )
            )
        return tuple(reasons)

    def card(self, reader: Reader, action: ActionRow, at: datetime) -> CardContent:
        params = action.params
        amount = int(params["amount_fen"])
        mandate = mandates.find_mandate(reader, str(params["mandate_ref"]))
        request = debit_requests.find_request(reader, str(params["request_id"]))
        account = accounts.find_account(reader, str(params["account_id"]))
        assert mandate is not None and request is not None and account is not None
        merchant = mandates.merchant_display(reader, mandate.mandate_ref)
        requests = debit_requests.mandate_requests(reader, mandate.mandate_ref)
        expected = mandate_rules.expected_amount(mandate, requests)
        day_sum = k3_policy.daily_sum(reader, action.customer_id, at.astimezone(SHANGHAI).date())
        headroom = compute_snapshot(
            cash_inputs_from_db(reader, action.customer_id, at)
        ).headroom_fen
        lines = [
            CardLine(key="merchant", label="商户", text=merchant),
            CardLine(key="amount", label="金额", text=fen_text(amount)),
            CardLine(key="cap", label="协议上限", text=fen_text(mandate.cap_fen)),
            CardLine(key="account", label="扣款账户", text=account.name),
            CardLine(key="period", label="期间", text=period_text(request.period_key)),
            CardLine(key="respond_by", label="回复截止", text=moment_text(request.respond_by)),
            CardLine(key="day_total", label="今日累计（含本笔）", text=fen_text(day_sum + amount)),
        ]
        sources = _sources(action)
        if request.description_raw:
            lines.append(
                CardLine(
                    key="merchant_text", label=MERCHANT_TEXT_LABEL, text=request.description_raw
                )
            )
            sources.append(
                SourceLabel(
                    slot="商户附言", provenance=Provenance.UNTRUSTED_TEXT, label=MERCHANT_TEXT_LABEL
                )
            )
        warnings = []
        if amount > expected:
            warnings.append(f"本次金额高于每月预计金额 {fen_text(expected)}")
        if action.tier is Tier.RED:
            warnings.append("今日累计超过 ¥1,000.00，确认后需要输入短信验证码。")
        over = amount - min(amount, expected)
        return CardContent(
            title=f"支付扣款请求：{merchant}",
            lines=tuple(lines),
            changes=(
                MoneyChange(
                    key="account_posted",
                    label=f"{account.name}余额",
                    before=account.balance_fen,
                    after=account.balance_fen - amount,
                ),
                MoneyChange(
                    key="headroom", label="可自由支配", before=headroom, after=headroom - over
                ),
            ),
            sources=tuple(sources),
            warnings=tuple(warnings),
        )

    def operation_terms(self, reader: Reader, action: ActionRow, at: datetime) -> OperationTerms:
        mandate = mandates.find_mandate(reader, str(action.params["mandate_ref"]))
        assert mandate is not None
        return OperationTerms(
            account_id=mandate.account_id,
            amount_fen=int(action.params["amount_fen"]),
            purpose_id=mandate.purpose_id,
            settle_due_at=at,
            settles_at_once=True,
        )

    def reserve(
        self, uow: UnitOfWork, operation: OperationRow, terms: OperationTerms
    ) -> ReservationRow:
        assert terms.amount_fen is not None
        return operations.insert_reservation(
            uow,
            operation_id=operation.operation_id,
            account_id=terms.account_id,
            amount_fen=terms.amount_fen,
            purpose_id=terms.purpose_id,
        )

    def apply(
        self, uow: UnitOfWork, operation: OperationRow, terms: OperationTerms, at: datetime
    ) -> BankRequest:
        assert operation.logical_action_id is not None and operation.revision is not None
        assert terms.amount_fen is not None
        params = _params_of(uow, operation)
        request = debit_requests.find_request(uow, str(params["request_id"]))
        mandate = mandates.find_mandate(uow, str(params["mandate_ref"]))
        assert request is not None and mandate is not None
        # The settlement account comes only from the registry, never from text or params.
        merchant = mandates.find_merchant(uow, mandate.merchant_id)
        assert merchant is not None
        order = MerchantPaymentOrder(
            key=operation.idempotency_key,
            operation_id=operation.operation_id,
            account_id=terms.account_id,
            amount_fen=terms.amount_fen,
            merchant_id=merchant.merchant_id,
            settlement_account_ref=merchant.settlement_account_ref,
            mandate_ref=mandate.mandate_ref,
            request_id=request.request_id,
            period_key=request.period_key,
        )
        return bank.submit_merchant_payment(uow, order, at)

    def settle(self, uow: UnitOfWork, operation: OperationRow, at: datetime) -> BankRequest:
        # Settled at submission; the stored order is returned unchanged.
        return bank.settle_payment(uow, operation.idempotency_key, at)

    def on_settled(self, uow: UnitOfWork, operation: OperationRow) -> dict[str, Any]:
        return {}

    def receipt_effect(self, operation: OperationRow, bank_request: BankRequest) -> dict[str, Any]:
        effect = bank_request.effect
        settled_at = bank_request.settled_at
        return {
            "account_id": operation.account_id,
            "amount_fen": operation.amount_fen,
            "settled_at": to_db(settled_at) if settled_at else None,
            "mandate_ref": effect["mandate_ref"],
            "request_id": effect["request_id"],
            "merchant_id": effect["merchant_id"],
            "period_key": effect["period_key"],
        }


def period_text(key: str) -> str:
    """2026年11月 for "2026-11"."""
    year, month = key.split("-")
    return f"{int(year)}年{int(month)}月"


def moment_text(moment: datetime) -> str:
    return moment.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")


def _params_of(reader: Reader, operation: OperationRow) -> Mapping[str, Any]:
    assert operation.logical_action_id is not None and operation.revision is not None
    return actions.get_revision(reader, operation.logical_action_id, operation.revision).params


def _sources(action: ActionRow) -> list[SourceLabel]:
    sources = []
    for slot, label in SLOT_LABELS.items():
        entry = action.provenance.get(slot)
        if not isinstance(entry, Mapping):
            continue
        provenance = Provenance(entry["provenance"])
        sources.append(
            SourceLabel(slot=label, provenance=provenance, label=SOURCE_LABELS[provenance])
        )
    return sources
