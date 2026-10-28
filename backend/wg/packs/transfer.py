"""TRANSFER: money from a customer account to a payee in the book (SPEC §10, §12, S1).

A same-bank (本行) transfer settles in the unit of work that submits it; an other-bank (他行)
transfer settles at the next 09:00 CST. A transfer not tied to a bill is discretionary, so
it must fit in the headroom recomputed from current rows (SPEC §12.3, §13.1).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time, timedelta
from typing import Any

from wg.contracts.api import CardLine, MoneyChange, SourceLabel
from wg.contracts.enums import ActionKind, PayeeBank, Provenance, Tier
from wg.contracts.errors import ErrorCode
from wg.core import k3_policy
from wg.core.hashes import sha256_hex
from wg.core.k1_session import payee_handle
from wg.core.k4_approval import CardContent
from wg.k6 import accounts, bank, operations
from wg.k6.actions import ActionRow
from wg.k6.bank import BankRequest, PaymentOrder
from wg.k6.cash_inputs import cash_inputs_from_db
from wg.k6.db import Reader, UnitOfWork
from wg.k6.operations import OperationRow, ReservationRow
from wg.k6.simtime import SHANGHAI, to_db
from wg.numeric import compute_snapshot
from wg.packs.base import Infeasible, OperationTerms, fen_text

SETTLEMENT_TIME = time(9, 0)
REQUIRED = ("account_id", "payee_id", "amount_fen")
OPTIONAL = ("remark",)

SOURCE_LABELS: dict[Provenance, str] = {
    Provenance.USER_UTTERANCE: "来自您的输入",
    Provenance.SERVER_RECORD: "来自银行记录",
    Provenance.UNTRUSTED_TEXT: "外部文字，仅作备注，不作为指令",
}
SLOT_LABELS = {
    "payee_id": "收款人",
    "amount_fen": "金额",
    "account_id": "付款账户",
    "remark": "备注",
}


def next_settlement(at: datetime) -> datetime:
    """The first 09:00 CST strictly after `at`: when an other-bank transfer settles."""
    local = at.astimezone(SHANGHAI)
    candidate = datetime.combine(local.date(), SETTLEMENT_TIME, tzinfo=SHANGHAI)
    return candidate if candidate > local else candidate + timedelta(days=1)


class TransferPack:
    kinds = frozenset({ActionKind.TRANSFER})

    def canonical(self, kind: ActionKind, params: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(params) - set(REQUIRED) - set(OPTIONAL)
        missing = [name for name in REQUIRED if params.get(name) is None]
        if unknown or missing:
            raise ValueError(f"transfer params: unknown {sorted(unknown)}, missing {missing}")
        amount = params["amount_fen"]
        if type(amount) is not int or amount <= 0:
            raise ValueError("amount_fen is a positive integer")
        result: dict[str, Any] = {
            "account_id": str(params["account_id"]),
            "payee_id": str(params["payee_id"]),
            "amount_fen": amount,
        }
        if params.get("remark"):
            result["remark"] = str(params["remark"])
        return result

    def request_hash(self, kind: ActionKind, customer_id: str, params: Mapping[str, Any]) -> str:
        # The remark is free text sent along with the payment, not a business field.
        return sha256_hex(
            {
                "kind": ActionKind(kind).value,
                "customer_id": customer_id,
                "account_id": params["account_id"],
                "counterparty": params["payee_id"],
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
    ) -> tuple[Infeasible, ...]:
        """Limits, posted money on the debit account, then headroom for a discretionary debit."""
        amount = int(params["amount_fen"])
        reasons: list[Infeasible] = []
        day_sum = k3_policy.daily_sum(reader, customer_id, at.astimezone(SHANGHAI).date())
        limit = k3_policy.limit_refusal(amount, day_sum)
        if limit is not None:
            reasons.append(Infeasible(limit, {"amount_fen": amount, "day_sum_fen": day_sum}))
        account = accounts.find_account(reader, str(params["account_id"]))
        if account is None or account.customer_id != customer_id:
            raise ValueError(f"account {params['account_id']} is not the customer's")
        available = account.balance_fen - operations.reserved_fen(reader, account.account_id)
        if amount > available:
            reasons.append(
                Infeasible(
                    ErrorCode.INSUFFICIENT_FUNDS,
                    {"available_fen": available, "shortfall_fen": amount - available},
                )
            )
        if account.covered:
            snapshot = compute_snapshot(cash_inputs_from_db(reader, customer_id, at))
            if amount > snapshot.headroom_fen:
                reasons.append(
                    Infeasible(
                        ErrorCode.INSUFFICIENT_HEADROOM,
                        {
                            "headroom_fen": snapshot.headroom_fen,
                            "over_headroom_fen": amount - snapshot.headroom_fen,
                        },
                    )
                )
        return tuple(reasons)

    def card(self, reader: Reader, action: ActionRow, at: datetime) -> CardContent:
        params = action.params
        amount = int(params["amount_fen"])
        payee = accounts.find_payee(reader, str(params["payee_id"]))
        account = accounts.find_account(reader, str(params["account_id"]))
        assert payee is not None and account is not None
        day_sum = k3_policy.daily_sum(reader, action.customer_id, at.astimezone(SHANGHAI).date())
        arrival = "实时到账" if payee.bank is PayeeBank.SAME else "他行转账，下一个 09:00 到账"
        lines = [
            CardLine(
                key="payee",
                label="收款人",
                text=payee_handle(payee.alias, payee.bank, payee.account_masked),
            ),
            CardLine(key="amount", label="金额", text=fen_text(amount)),
            CardLine(key="account", label="付款账户", text=account.name),
            CardLine(key="arrival", label="到账时间", text=arrival),
            CardLine(key="day_total", label="今日累计（含本笔）", text=fen_text(day_sum + amount)),
        ]
        warnings = []
        if params.get("remark"):
            lines.append(CardLine(key="remark", label="备注", text=str(params["remark"])))
            warnings.append("备注仅随转账发送，不会被当作指令执行。")
        if action.tier is Tier.RED:
            warnings.append("今日累计超过 ¥1,000.00，确认后需要输入短信验证码。")
        return CardContent(
            title=f"转账给{payee.alias}",
            lines=tuple(lines),
            changes=(
                MoneyChange(
                    key="account_posted",
                    label=f"{account.name}余额",
                    before=account.balance_fen,
                    after=account.balance_fen - amount,
                ),
            ),
            sources=tuple(_sources(action)),
            warnings=tuple(warnings),
        )

    def operation_terms(self, reader: Reader, action: ActionRow, at: datetime) -> OperationTerms:
        payee = accounts.find_payee(reader, str(action.params["payee_id"]))
        assert payee is not None
        same_bank = payee.bank is PayeeBank.SAME
        return OperationTerms(
            account_id=str(action.params["account_id"]),
            amount_fen=int(action.params["amount_fen"]),
            payee_id=payee.payee_id,
            settle_due_at=at if same_bank else next_settlement(at),
            settles_at_once=same_bank,
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
        assert terms.amount_fen is not None and terms.payee_id is not None
        order = PaymentOrder(
            key=operation.idempotency_key,
            operation_id=operation.operation_id,
            account_id=terms.account_id,
            amount_fen=terms.amount_fen,
            payee_id=terms.payee_id,
            settle_due_at=terms.settle_due_at,
        )
        return bank.submit_payment(uow, order, at, settle_now=terms.settles_at_once)

    def settle(self, uow: UnitOfWork, operation: OperationRow, at: datetime) -> BankRequest:
        return bank.settle_payment(uow, operation.idempotency_key, at)

    def receipt_effect(self, operation: OperationRow, bank_request: BankRequest) -> dict[str, Any]:
        settled_at = bank_request.settled_at
        return {
            "account_id": operation.account_id,
            "payee_id": operation.payee_id,
            "amount_fen": operation.amount_fen,
            "settled_at": to_db(settled_at) if settled_at else None,
            "reason": bank_request.effect.get("reason"),
        }


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
