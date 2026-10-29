"""TRANSFER and PAY_BILL: money from a customer account to a payee in the book (SPEC §10, §12).

A same-bank (本行) transfer settles in the unit of work that submits it; an other-bank (他行)
transfer settles at the next 09:00 CST. A transfer not tied to a bill is discretionary, so
it must fit in the headroom recomputed from current rows (SPEC §12.3, §13.1).

PAY_BILL is a transfer to the payee of a CONFIRMED or PARTIALLY_PAID bill for at most its
unpaid amount. It is paid from the bill's own protection, so headroom is not checked; its
hold shares the bill's purpose and counts once. Settlement lowers the bill's unpaid amount
(SPEC §12.4).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time, timedelta
from typing import Any

from wg.contracts.api import CardLine, MoneyChange, SourceLabel
from wg.contracts.enums import ActionKind, CommitmentStatus, PayeeBank, Provenance, Tier
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.events import CommitmentChange
from wg.core import k3_policy
from wg.core.hashes import sha256_hex
from wg.core.k1_session import payee_handle
from wg.core.k4_approval import CardContent
from wg.k6 import accounts, bank, commitments, operations
from wg.k6.actions import ActionRow
from wg.k6.bank import BankRequest, PaymentOrder
from wg.k6.cash_inputs import cash_inputs_from_db
from wg.k6.db import Reader, UnitOfWork
from wg.k6.operations import OperationRow, ReservationRow
from wg.k6.simtime import SHANGHAI, to_db
from wg.numeric import compute_snapshot
from wg.packs.base import Infeasible, OperationTerms, fen_text
from wg.packs.labels import SOURCE_LABELS, bill_label

SETTLEMENT_TIME = time(9, 0)
REQUIRED = ("account_id", "payee_id", "amount_fen")
BILL_REQUIRED = (*REQUIRED, "commitment_id")
OPTIONAL = ("remark",)

SLOT_LABELS = {
    "payee_id": "收款人",
    "amount_fen": "金额",
    "account_id": "付款账户",
    "remark": "备注",
    "commitment_id": "账单",
}


def next_settlement(at: datetime) -> datetime:
    """The first 09:00 CST strictly after `at`: when an other-bank transfer settles."""
    local = at.astimezone(SHANGHAI)
    candidate = datetime.combine(local.date(), SETTLEMENT_TIME, tzinfo=SHANGHAI)
    return candidate if candidate > local else candidate + timedelta(days=1)


class TransferPack:
    kinds = frozenset({ActionKind.TRANSFER, ActionKind.PAY_BILL})

    def canonical(self, kind: ActionKind, params: Mapping[str, Any]) -> dict[str, Any]:
        is_bill = ActionKind(kind) is ActionKind.PAY_BILL
        required = BILL_REQUIRED if is_bill else REQUIRED
        allowed = set(required) | (set() if is_bill else set(OPTIONAL))
        unknown = set(params) - allowed
        missing = [name for name in required if params.get(name) is None]
        if unknown or missing:
            raise ValueError(f"{kind} params: unknown {sorted(unknown)}, missing {missing}")
        amount = params["amount_fen"]
        if type(amount) is not int or amount <= 0:
            raise ValueError("amount_fen is a positive integer")
        result: dict[str, Any] = {
            "account_id": str(params["account_id"]),
            "payee_id": str(params["payee_id"]),
            "amount_fen": amount,
        }
        if is_bill:
            result["commitment_id"] = str(params["commitment_id"])
        elif params.get("remark"):
            result["remark"] = str(params["remark"])
        return result

    def request_hash(self, kind: ActionKind, customer_id: str, params: Mapping[str, Any]) -> str:
        # The remark is free text sent along with the payment, not a business field.
        business = {
            "kind": ActionKind(kind).value,
            "customer_id": customer_id,
            "account_id": params["account_id"],
            "counterparty": params["payee_id"],
            "amount_fen": params["amount_fen"],
        }
        if params.get("commitment_id"):
            business["commitment_id"] = params["commitment_id"]
        return sha256_hex(business)

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
        """Bill checks, limits, posted money on the debit account, headroom if discretionary."""
        amount = int(params["amount_fen"])
        reasons: list[Infeasible] = []
        is_bill = ActionKind(kind) is ActionKind.PAY_BILL
        if is_bill:
            reasons.extend(bill_refusals(reader, customer_id, params))
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
        if account.covered and not is_bill:
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
        bill = _bill_of(reader, action)
        if bill is not None:
            lines.insert(
                0,
                CardLine(
                    key="bill", label="账单", text=f"{bill_label(bill.kind)}（{bill.due_date}到期）"
                ),
            )
            lines.append(
                CardLine(
                    key="unpaid_after", label="付款后待付", text=fen_text(bill.unpaid_fen - amount)
                )
            )
        if params.get("remark"):
            lines.append(CardLine(key="remark", label="备注", text=str(params["remark"])))
            warnings.append("备注仅随转账发送，不会被当作指令执行。")
        if action.tier is Tier.RED:
            warnings.append("今日累计超过 ¥1,000.00，确认后需要输入短信验证码。")
        title = f"转账给{payee.alias}" if bill is None else f"支付账单：{bill_label(bill.kind)}"
        return CardContent(
            title=title,
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
        bill = _bill_of(reader, action)
        same_bank = payee.bank is PayeeBank.SAME
        return OperationTerms(
            account_id=str(action.params["account_id"]),
            amount_fen=int(action.params["amount_fen"]),
            payee_id=payee.payee_id,
            commitment_id=bill.commitment_id if bill else None,
            purpose_id=bill.purpose_id if bill else None,
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

    def on_settled(self, uow: UnitOfWork, operation: OperationRow) -> dict[str, Any]:
        """A bill payment lowers the unpaid amount and may close the bill (SPEC §12.4)."""
        if operation.commitment_id is None:
            return {}
        assert operation.amount_fen is not None
        bill = commitments.get_commitment(uow, operation.commitment_id)
        unpaid = bill.unpaid_fen - operation.amount_fen
        if unpaid < 0:
            raise WgError(
                ErrorCode.INTEGRITY_VIOLATION,
                {
                    "reason": "unpaid_mismatch",
                    "operation_id": operation.operation_id,
                    "commitment_id": bill.commitment_id,
                },
            )
        paid_in_full = unpaid == 0
        commitments.transition(
            uow,
            bill,
            to=CommitmentStatus.PAID if paid_in_full else CommitmentStatus.PARTIALLY_PAID,
            change=CommitmentChange.PAID if paid_in_full else CommitmentChange.PARTIALLY_PAID,
            delta_fen=-operation.amount_fen,
            unpaid_fen=unpaid,
            source_ref=operation.operation_id,
        )
        return {"commitment_id": bill.commitment_id, "unpaid_after_fen": unpaid}

    def receipt_effect(self, operation: OperationRow, bank_request: BankRequest) -> dict[str, Any]:
        settled_at = bank_request.settled_at
        return {
            "account_id": operation.account_id,
            "payee_id": operation.payee_id,
            "amount_fen": operation.amount_fen,
            "settled_at": to_db(settled_at) if settled_at else None,
            "reason": bank_request.effect.get("reason"),
        }


def bill_refusals(reader: Reader, customer_id: str, params: Mapping[str, Any]) -> list[Infeasible]:
    """The bill checks of a PAY_BILL (SPEC §12.3 step 4), after the unpaid-amount invariant.

    A payment still in flight counts against the unpaid amount, so two payments cannot
    together exceed the bill.
    """
    commitments.check_unpaid_invariant(reader, customer_id)
    bill = commitments.find_commitment(reader, str(params["commitment_id"]))
    if bill is None or bill.customer_id != customer_id:
        raise ValueError(f"commitment {params['commitment_id']} is not the customer's")
    if bill.status not in commitments.OWED:
        return [Infeasible(ErrorCode.BILL_NOT_PAYABLE, {"status": bill.status.value})]
    reasons = []
    if params["payee_id"] != bill.payee_id:
        reasons.append(Infeasible(ErrorCode.PAYEE_MISMATCH, {"commitment_id": bill.commitment_id}))
    payable = bill.unpaid_fen - commitments.open_payment_fen(reader, bill.commitment_id)
    if int(params["amount_fen"]) > payable:
        reasons.append(
            Infeasible(
                ErrorCode.AMOUNT_OVER_UNPAID,
                {"unpaid_fen": bill.unpaid_fen, "payable_fen": max(0, payable)},
            )
        )
    return reasons


def _bill_of(reader: Reader, action: ActionRow) -> commitments.CommitmentRow | None:
    commitment_id = action.params.get("commitment_id")
    return None if commitment_id is None else commitments.get_commitment(reader, commitment_id)


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
