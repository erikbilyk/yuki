"""DEBIT_PAY and DEBIT_DECLINE: the customer answers one merchant request (S5-SPEC §6).

A payment is checked by the payment gate of S5-SPEC §5.4 at the draft, at the approval
request and inside the submitting unit of work: the agreement's event history first, then
the request (visible, awaiting the customer, before `respond_by`), the agreement (the
version on the card, ACTIVE, unexpired, the cap), the period (not paid yet, no payment in
flight), and finally the money (limits, posted money, headroom). The first failure of the
request or the agreement decides; the money reasons are listed like a transfer's.

A payment settles in the unit that submits it. It draws on the protection of its period up
to the expected amount; only the part above that must fit in the headroom.

A decline moves no money: it marks the request DECLINED, which occupies the period, and the
period stays protected until `respond_by` (S5-SPEC §5.5, §5.6).

A new agreement (MANDATE_CREATE) is the other way round: the customer asks for it, so the
merchant must be in the registry and verified, the term at most 12 months and the cap not
below the expected amount. It starts on the day it is confirmed and runs `months` months;
from then on its first period is protected like any other (S5-SPEC §5.6, §6.1).

A change of an agreement (cancel, suspend, resume, lower or raise the cap) moves no money
either, but goes through the gateway like a payment, so it leaves an operation and one
receipt: the bank records it and applies it, with what it does to the pending requests,
in the submitting unit (S5-SPEC §5.3, §6.2). It is checked again there: the agreement's
event history, the version on the card, a status the change can start from, the expiry
and the direction of a cap change.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from wg.contracts.api import CardLine, MoneyChange, SourceLabel
from wg.contracts.engines import CashInputs, CommitmentInput
from wg.contracts.enums import (
    ActionKind,
    ActionStatus,
    CommitmentStatus,
    DebitRequestStatus,
    MandateStatus,
    Provenance,
    Tier,
)
from wg.contracts.errors import ErrorCode
from wg.contracts.ids import IdPrefix, new_id
from wg.core import k3_policy, mandate_rules
from wg.core.hashes import sha256_hex
from wg.core.k1_session import not_found
from wg.core.k4_approval import CardContent
from wg.k6 import accounts, actions, bank, debit_requests, mandates, operations
from wg.k6.actions import ActionRow
from wg.k6.bank import BankRequest, MandateChangeOrder, MandateCreateOrder, MerchantPaymentOrder
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
DECLINE_PARAMS = ("mandate_ref", "request_id")
# A decision action still waiting for the customer; it can be refused as a duplicate or
# closed when its request or agreement moves on (S5-SPEC §5.8 step 3).
OPEN_DECISION_STATUSES = frozenset(
    {
        ActionStatus.DRAFT,
        ActionStatus.AWAITING_APPROVAL,
        ActionStatus.AWAITING_STEPUP,
        ActionStatus.READY,
    }
)
DECLINE_WARNINGS = (
    "拒绝后本月不再接受该商户的扣款请求",
    "本期预留资金在回复截止后释放",
    "商户可能因此暂停服务",
)

STATUS_CHANGE_PARAMS = ("mandate_ref", "mandate_version")
CAP_CHANGE_PARAMS = ("mandate_ref", "mandate_version", "cap_fen")
CHANGE_TITLES = {
    ActionKind.MANDATE_CANCEL: "关闭自动扣款",
    ActionKind.MANDATE_SUSPEND: "暂停自动扣款",
    ActionKind.MANDATE_RESUME: "恢复自动扣款",
    ActionKind.MANDATE_CAP_LOWER: "调低扣款上限",
    ActionKind.MANDATE_CAP_RAISE: "调高扣款上限",
}
CAP_WARNINGS = {
    ActionKind.MANDATE_CAP_LOWER: "高于新上限的等待中请求将转为超出协议",
    ActionKind.MANDATE_CAP_RAISE: (
        "超出原上限的待处理请求会重新检查，每期最多一笔转为待您确认，不会自动扣款"
    ),
}
RESUME_WARNING = "恢复后商户可以再次发起扣款请求，每一笔仍需您确认"
WAITING_DECLINED_WARNING = "等待中的扣款请求将被自动拒绝"
CODE_WARNING = "确认后需要输入短信验证码。"

CREATE_PARAMS = ("account_id", "merchant_id", "expected_fen", "cap_fen", "debit_day", "months")
CREATE_SLOT_LABELS = {
    "merchant_id": "商户",
    "expected_fen": "每月预计",
    "cap_fen": "扣款上限",
    "debit_day": "扣款日",
    "months": "期限",
}
CREATE_WARNING = "未在回复截止前处理的请求不会扣款"
# Stand-ins for the ids a stored agreement gets, so the card can show its protection.
_PREVIEW_REF = "mdt_preview"
_PREVIEW_PURPOSE = "pur_preview"

SLOT_LABELS = {"request_id": "扣款请求", "amount_fen": "金额", "account_id": "扣款账户"}
CHANGE_SLOT_LABELS = {"mandate_ref": "自动扣款协议", "cap_fen": "扣款上限"}
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
    """Why the customer can no longer answer the request, if so (S5-SPEC §5.4, §5.5)."""
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


class DebitDeclinePack:
    kinds = frozenset({ActionKind.DEBIT_DECLINE})

    def canonical(self, kind: ActionKind, params: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(params) - set(DECLINE_PARAMS)
        missing = [name for name in DECLINE_PARAMS if params.get(name) is None]
        if unknown or missing:
            raise ValueError(f"{kind} params: unknown {sorted(unknown)}, missing {missing}")
        return {name: str(params[name]) for name in DECLINE_PARAMS}

    def request_hash(self, kind: ActionKind, customer_id: str, params: Mapping[str, Any]) -> str:
        return sha256_hex(
            {
                "kind": ActionKind(kind).value,
                "customer_id": customer_id,
                "request_id": params["request_id"],
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
        return k3_policy.tier_for(kind)

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
        """The agreement's integrity, then a request the customer can still answer."""
        _, request = decision_target(
            reader, customer_id, str(params["mandate_ref"]), str(params["request_id"])
        )
        refusal = answer_refusal(request, at)
        return () if refusal is None else (refusal,)

    def card(self, reader: Reader, action: ActionRow, at: datetime) -> CardContent:
        request = debit_requests.find_request(reader, str(action.params["request_id"]))
        assert request is not None and request.mandate_ref is not None
        merchant = mandates.merchant_display(reader, request.mandate_ref)
        return CardContent(
            title=f"拒绝扣款请求：{merchant}",
            lines=(
                CardLine(key="merchant", label="商户", text=merchant),
                CardLine(key="amount", label="金额", text=fen_text(request.amount_fen)),
                CardLine(key="period", label="期间", text=period_text(request.period_key)),
            ),
            sources=tuple(_sources(action)),
            warnings=DECLINE_WARNINGS,
        )

    def execute(self, uow: UnitOfWork, action: ActionRow, at: datetime) -> None:
        debit_requests.decline(uow, str(action.params["request_id"]))


def _changed(reason: str, mandate: MandateRow) -> Infeasible:
    detail: dict[str, Any] = {"reason": reason, "mandate_ref": mandate.mandate_ref}
    if reason == "status":
        detail["status"] = mandate.status.value
    return Infeasible(ErrorCode.MANDATE_CHANGED, detail)


def change_target(reader: Reader, customer_id: str, mandate_ref: str) -> MandateRow:
    """The customer's agreement after its integrity check; foreign or unknown is NOT_FOUND."""
    mandate = mandates.find_mandate(reader, mandate_ref)
    if mandate is None or mandate.customer_id != customer_id:
        raise not_found()
    return mandates.check_mandate_invariant(reader, mandate_ref)


def change_refusal(
    mandate: MandateRow, kind: ActionKind, params: Mapping[str, Any], at: datetime
) -> Infeasible | None:
    """Why the change can no longer be made to the agreement as it is now (S5-SPEC §6.2)."""
    if mandate.version != params["mandate_version"]:
        return _changed("version", mandate)
    if mandate.status not in mandate_rules.CHANGE_FROM[kind]:
        return _changed("status", mandate)
    if not mandate_rules.authorizes(mandate.expires_on, at):
        return _changed("expired", mandate)
    if kind in mandate_rules.CAP_KINDS:
        cap_fen = int(params["cap_fen"])
        if mandate_rules.cap_change_kind(mandate.cap_fen, cap_fen) is not kind:
            # Unreachable while the version matches: the draft took the kind from this cap.
            return _changed("version", mandate)
        if cap_fen > k3_policy.SINGLE_TRANSFER_LIMIT_FEN:
            return Infeasible(ErrorCode.SINGLE_LIMIT_EXCEEDED, {"cap_fen": cap_fen})
    return None


class MandateChangePack:
    """Cancel, suspend, resume and cap changes of an agreement: no reservation, no ledger."""

    kinds = mandate_rules.CHANGE_KINDS

    def canonical(self, kind: ActionKind, params: Mapping[str, Any]) -> dict[str, Any]:
        names = CAP_CHANGE_PARAMS if kind in mandate_rules.CAP_KINDS else STATUS_CHANGE_PARAMS
        unknown = set(params) - set(names)
        missing = [name for name in names if params.get(name) is None]
        if unknown or missing:
            raise ValueError(f"{kind} params: unknown {sorted(unknown)}, missing {missing}")
        for name in names[1:]:
            if type(params[name]) is not int or params[name] <= 0:
                raise ValueError(f"{name} is a positive integer")
        return {"mandate_ref": str(params["mandate_ref"])} | {
            name: params[name] for name in names[1:]
        }

    def request_hash(self, kind: ActionKind, customer_id: str, params: Mapping[str, Any]) -> str:
        fields = {
            "kind": ActionKind(kind).value,
            "customer_id": customer_id,
            "mandate_ref": params["mandate_ref"],
            "mandate_version": params["mandate_version"],
        }
        if kind in mandate_rules.CAP_KINDS:
            fields["cap_fen"] = params["cap_fen"]
        return sha256_hex(fields)

    def tier(
        self,
        reader: Reader,
        customer_id: str,
        kind: ActionKind,
        params: Mapping[str, Any],
        at: datetime,
    ) -> Tier:
        return k3_policy.tier_for(kind)

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
        """The agreement's integrity, then version, status, expiry and cap direction."""
        mandate = change_target(reader, customer_id, str(params["mandate_ref"]))
        refusal = change_refusal(mandate, ActionKind(kind), params, at)
        return () if refusal is None else (refusal,)

    def card(self, reader: Reader, action: ActionRow, at: datetime) -> CardContent:
        kind = action.kind
        mandate = mandates.find_mandate(reader, str(action.params["mandate_ref"]))
        assert mandate is not None
        merchant = mandates.merchant_display(reader, mandate.mandate_ref)
        requests = debit_requests.mandate_requests(reader, mandate.mandate_ref)
        expected = mandate_rules.expected_amount(mandate, requests)
        next_due = mandate_rules.next_debit_date(mandate, at.astimezone(SHANGHAI).date())
        lines = (
            CardLine(key="merchant", label="商户", text=merchant),
            CardLine(key="expected", label="每月预计", text=fen_text(expected)),
            CardLine(key="cap", label="上限", text=fen_text(mandate.cap_fen)),
            CardLine(
                key="next_debit_date",
                label="下次扣款日",
                text=next_due.isoformat() if next_due else "无",
            ),
            CardLine(key="expires_on", label="有效期至", text=mandate.expires_on.isoformat()),
        )
        waiting = any(r.status is DebitRequestStatus.AWAITING_CLIENT for r in requests)
        warnings: list[str] = []
        changes: tuple[MoneyChange, ...] = ()
        if kind is ActionKind.MANDATE_CANCEL:
            warnings += ["关闭后该商户的扣款请求将被自动拒绝", "与商户的服务合同不会因此解除"]
            kept = _kept_after_cancel(mandate, requests, at)
            if kept is not None:
                warnings.append(f"本期预留的 {fen_text(kept[0])} 保留到 {kept[1]}，之后释放")
        elif kind is ActionKind.MANDATE_SUSPEND:
            warnings += [
                "暂停后该商户的扣款请求将被自动拒绝",
                "与商户的服务合同不会因此解除",
                "暂停不会自动恢复；恢复需要您确认并输入短信验证码",
            ]
        elif kind is ActionKind.MANDATE_RESUME:
            warnings.append(RESUME_WARNING)
        else:
            changes = (
                MoneyChange(
                    key="cap",
                    label="扣款上限",
                    before=mandate.cap_fen,
                    after=int(action.params["cap_fen"]),
                ),
            )
            warnings.append(CAP_WARNINGS[kind])
        if waiting and kind in (ActionKind.MANDATE_CANCEL, ActionKind.MANDATE_SUSPEND):
            warnings.append(WAITING_DECLINED_WARNING)
        if action.tier is Tier.RED:
            warnings.append(CODE_WARNING)
        return CardContent(
            title=f"{CHANGE_TITLES[kind]}：{merchant}",
            lines=lines,
            changes=changes,
            sources=tuple(_sources(action, CHANGE_SLOT_LABELS)),
            warnings=tuple(warnings),
        )

    def operation_terms(self, reader: Reader, action: ActionRow, at: datetime) -> OperationTerms:
        mandate = mandates.find_mandate(reader, str(action.params["mandate_ref"]))
        assert mandate is not None
        return OperationTerms(account_id=mandate.account_id, settle_due_at=at, settles_at_once=True)

    def reserve(self, uow: UnitOfWork, operation: OperationRow, terms: OperationTerms) -> None:
        return None

    def apply(
        self, uow: UnitOfWork, operation: OperationRow, terms: OperationTerms, at: datetime
    ) -> BankRequest:
        params = _params_of(uow, operation)
        cap_fen = params.get("cap_fen")
        order = MandateChangeOrder(
            key=operation.idempotency_key,
            operation_id=operation.operation_id,
            kind=ActionKind(operation.action_kind),
            mandate_ref=str(params["mandate_ref"]),
            mandate_version=int(params["mandate_version"]),
            cap_fen=int(cap_fen) if cap_fen is not None else None,
        )
        return bank.submit_mandate_change(uow, order, at)

    def settle(self, uow: UnitOfWork, operation: OperationRow, at: datetime) -> BankRequest:
        # Settled at submission; the stored record is returned unchanged.
        request = bank.find_request(uow, operation.idempotency_key)
        if request is None:
            raise LookupError(f"no bank request {operation.idempotency_key}")
        return request

    def on_settled(self, uow: UnitOfWork, operation: OperationRow) -> dict[str, Any]:
        return {}

    def receipt_effect(self, operation: OperationRow, bank_request: BankRequest) -> dict[str, Any]:
        effect = bank_request.effect
        return {name: effect[name] for name in ("mandate_ref", "kind", "field", "old", "new")}


def _refusal(reason: str) -> Infeasible:
    return Infeasible(ErrorCode.UNSUPPORTED_REQUEST, {"reason": reason})


def create_refusal(
    reader: Reader, customer_id: str, params: Mapping[str, Any]
) -> Infeasible | None:
    """Why the agreement cannot be opened as asked, in the order of S5-SPEC §6.3.

    An unknown merchant or a foreign account is NOT_FOUND. The gateway runs this check at the
    draft, at the approval request and at submission, so a merchant that stops being
    verified in between refuses.
    """
    account = accounts.find_account(reader, str(params["account_id"]))
    if account is None or account.customer_id != customer_id:
        raise not_found()
    merchant = mandates.find_merchant(reader, str(params["merchant_id"]))
    if merchant is None:
        raise not_found()
    if not merchant.verified:
        return _refusal("merchant_unverified")
    if params["months"] > mandate_rules.MAX_TERM_MONTHS:
        return _refusal("term_over_12_months")
    if not 1 <= params["debit_day"] <= 31:
        return _refusal("day_out_of_range")
    if params["cap_fen"] > k3_policy.SINGLE_TRANSFER_LIMIT_FEN:
        return Infeasible(ErrorCode.SINGLE_LIMIT_EXCEEDED, {"cap_fen": params["cap_fen"]})
    return None


def prospective_mandate(
    customer_id: str, params: Mapping[str, Any], at: datetime, *, ref: str, purpose_id: str
) -> MandateRow:
    """The agreement the params would create when confirmed at `at`: ACTIVE, from today."""
    today = at.astimezone(SHANGHAI).date()
    return MandateRow(
        mandate_ref=ref,
        customer_id=customer_id,
        account_id=str(params["account_id"]),
        merchant_id=str(params["merchant_id"]),
        purpose_id=purpose_id,
        expected_fen=int(params["expected_fen"]),
        cap_fen=int(params["cap_fen"]),
        debit_day=int(params["debit_day"]),
        starts_on=today,
        expires_on=mandate_rules.term_end(today, int(params["months"])),
        status=MandateStatus.ACTIVE,
        cancelled_at=None,
        version=1,
    )


def _with_periods(inputs: CashInputs, mandate: MandateRow, at: datetime) -> CashInputs:
    """The inputs with the new agreement's protected periods added to the commitments."""
    periods = tuple(
        CommitmentInput(
            commitment_id=mandate_rules.subscription_commitment_id(
                mandate.mandate_ref, period.period_key
            ),
            purpose_id=mandate.purpose_id,
            status=CommitmentStatus.CONFIRMED,
            amount_fen=period.expected_fen,
            unpaid_fen=period.expected_fen,
            due_date=period.due_date,
        )
        for period in mandate_rules.protected_periods(mandate, [], at)
    )
    return inputs.model_copy(update={"commitments": inputs.commitments + periods})


class MandateCreatePack:
    """Opens an agreement with a registry merchant: no reservation, no ledger entry."""

    kinds = frozenset({ActionKind.MANDATE_CREATE})

    def canonical(self, kind: ActionKind, params: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(params) - set(CREATE_PARAMS)
        missing = [name for name in CREATE_PARAMS if params.get(name) is None]
        if unknown or missing:
            raise ValueError(f"{kind} params: unknown {sorted(unknown)}, missing {missing}")
        result: dict[str, Any] = {
            "account_id": str(params["account_id"]),
            "merchant_id": str(params["merchant_id"]),
        }
        for name in CREATE_PARAMS[2:]:
            if type(params[name]) is not int or params[name] <= 0:
                raise ValueError(f"{name} is a positive integer")
            result[name] = params[name]
        if result["cap_fen"] < result["expected_fen"]:
            raise ValueError("cap_fen must not be below expected_fen")
        return result

    def request_hash(self, kind: ActionKind, customer_id: str, params: Mapping[str, Any]) -> str:
        return sha256_hex(
            {"kind": ActionKind(kind).value, "customer_id": customer_id}
            | {name: params[name] for name in CREATE_PARAMS}
        )

    def tier(
        self,
        reader: Reader,
        customer_id: str,
        kind: ActionKind,
        params: Mapping[str, Any],
        at: datetime,
    ) -> Tier:
        return k3_policy.tier_for(kind)

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
        refusal = create_refusal(reader, customer_id, params)
        return () if refusal is None else (refusal,)

    def card(self, reader: Reader, action: ActionRow, at: datetime) -> CardContent:
        params = action.params
        merchant = mandates.find_merchant(reader, str(params["merchant_id"]))
        assert merchant is not None
        agreement = prospective_mandate(
            action.customer_id, params, at, ref=_PREVIEW_REF, purpose_id=_PREVIEW_PURPOSE
        )
        today = at.astimezone(SHANGHAI).date()
        next_due = mandate_rules.next_debit_date(agreement, today)
        inputs = cash_inputs_from_db(reader, action.customer_id, at)
        before = compute_snapshot(inputs)
        after = compute_snapshot(_with_periods(inputs, agreement, at))
        return CardContent(
            title=f"新增自动扣款：{merchant.display_name}",
            lines=(
                CardLine(key="merchant", label="商户", text=merchant.display_name),
                CardLine(key="expected", label="每月预计", text=fen_text(agreement.expected_fen)),
                CardLine(key="cap", label="上限", text=fen_text(agreement.cap_fen)),
                CardLine(key="debit_day", label="扣款日", text=f"每月{agreement.debit_day}日"),
                CardLine(key="expires_on", label="有效期至", text=agreement.expires_on.isoformat()),
                CardLine(
                    key="next_debit_date",
                    label="下次扣款日",
                    text=next_due.isoformat() if next_due else "无",
                ),
            ),
            changes=(
                MoneyChange(
                    key="protected",
                    label="受保护金额",
                    before=before.protected_fen,
                    after=after.protected_fen,
                ),
                MoneyChange(
                    key="headroom",
                    label="可自由支配",
                    before=before.headroom_fen,
                    after=after.headroom_fen,
                ),
            ),
            sources=tuple(_sources(action, CREATE_SLOT_LABELS)),
            warnings=(CREATE_WARNING, CODE_WARNING),
        )

    def operation_terms(self, reader: Reader, action: ActionRow, at: datetime) -> OperationTerms:
        return OperationTerms(
            account_id=str(action.params["account_id"]), settle_due_at=at, settles_at_once=True
        )

    def reserve(self, uow: UnitOfWork, operation: OperationRow, terms: OperationTerms) -> None:
        return None

    def apply(
        self, uow: UnitOfWork, operation: OperationRow, terms: OperationTerms, at: datetime
    ) -> BankRequest:
        assert operation.customer_id is not None
        params = _params_of(uow, operation)
        agreement = prospective_mandate(
            operation.customer_id,
            params,
            at,
            ref=new_id(IdPrefix.MANDATE),
            purpose_id=new_id(IdPrefix.PURPOSE),
        )
        order = MandateCreateOrder(
            key=operation.idempotency_key,
            operation_id=operation.operation_id,
            customer_id=agreement.customer_id,
            account_id=agreement.account_id,
            merchant_id=agreement.merchant_id,
            mandate_ref=agreement.mandate_ref,
            purpose_id=agreement.purpose_id,
            expected_fen=agreement.expected_fen,
            cap_fen=agreement.cap_fen,
            debit_day=agreement.debit_day,
            starts_on=agreement.starts_on,
            expires_on=agreement.expires_on,
        )
        return bank.submit_mandate_creation(uow, order, at)

    def settle(self, uow: UnitOfWork, operation: OperationRow, at: datetime) -> BankRequest:
        # Settled at submission; the stored record is returned unchanged.
        request = bank.find_request(uow, operation.idempotency_key)
        if request is None:
            raise LookupError(f"no bank request {operation.idempotency_key}")
        return request

    def on_settled(self, uow: UnitOfWork, operation: OperationRow) -> dict[str, Any]:
        return {}

    def receipt_effect(self, operation: OperationRow, bank_request: BankRequest) -> dict[str, Any]:
        effect = bank_request.effect
        return {
            name: effect[name] for name in ("mandate_ref", "merchant_id", "starts_on", "expires_on")
        }


def _kept_after_cancel(
    mandate: MandateRow, requests: list[DebitRequestRow], at: datetime
) -> tuple[int, str] | None:
    """The amount and last day of the protection a cancel at `at` leaves in place, if any.

    Requests awaiting the customer are declined by the cancel, so they protect nothing.
    """
    cancelled = dataclasses.replace(mandate, status=MandateStatus.CANCELLED, cancelled_at=at)
    remaining = [r for r in requests if r.status is not DebitRequestStatus.AWAITING_CLIENT]
    for period in mandate_rules.protected_periods(cancelled, remaining, at):
        if period.until is not None:
            last_day = (period.until - timedelta(microseconds=1)).astimezone(SHANGHAI).date()
            return period.expected_fen, last_day.isoformat()
    return None


def period_text(key: str) -> str:
    """2026年11月 for "2026-11"."""
    year, month = key.split("-")
    return f"{int(year)}年{int(month)}月"


def moment_text(moment: datetime) -> str:
    return moment.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")


def _params_of(reader: Reader, operation: OperationRow) -> Mapping[str, Any]:
    assert operation.logical_action_id is not None and operation.revision is not None
    return actions.get_revision(reader, operation.logical_action_id, operation.revision).params


def _sources(action: ActionRow, labels: Mapping[str, str] = SLOT_LABELS) -> list[SourceLabel]:
    sources = []
    for slot, label in labels.items():
        entry = action.provenance.get(slot)
        if not isinstance(entry, Mapping):
            continue
        provenance = Provenance(entry["provenance"])
        sources.append(
            SourceLabel(slot=label, provenance=provenance, label=SOURCE_LABELS[provenance])
        )
    return sources
