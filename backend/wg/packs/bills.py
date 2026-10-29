"""Actions on the customer's own records of obligations: reserve, bills, statement lines (S2).

None of these moves money: the gateway runs `execute` in the submitting unit of work and
leaves no operation, bank call or receipt. Whatever raises the protected amount is YELLOW and
whatever lowers it is RED (SPEC §10.1, S2 §8): a reserve increase and CONFIRM_BILL are YELLOW,
a reserve decrease, CANCEL_BILL and MARK_PAID_EXTERNAL are RED. RESOLVE_ITEM changes how a
statement line is classified, not the protection, and is GREEN.

Every card shows what the action does to the protected and the free amount, computed by the
cash engine on the current rows with the change applied.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from wg.contracts.api import CardLine, MoneyChange, SourceLabel
from wg.contracts.engines import CashInputs, CashSnapshotValues
from wg.contracts.enums import ActionKind, CommitmentStatus, Provenance, Tier
from wg.contracts.errors import ErrorCode
from wg.contracts.events import CommitmentChange
from wg.core import k3_policy
from wg.core.hashes import sha256_hex
from wg.core.k1_session import payee_handle
from wg.core.k4_approval import CardContent
from wg.k6 import accounts, commitments, imports, reserves
from wg.k6.actions import ActionRow
from wg.k6.cash_inputs import cash_inputs_from_db
from wg.k6.categories import CATEGORIES
from wg.k6.commitments import CommitmentRow
from wg.k6.db import Reader, UnitOfWork
from wg.numeric import compute_snapshot
from wg.packs.base import Infeasible, fen_text
from wg.packs.labels import BILL_SOURCE_PROVENANCE, SOURCE_LABELS, bill_label

_K = ActionKind

# The params each kind takes, all required.
PARAMS: Mapping[ActionKind, tuple[str, ...]] = {
    _K.DESIGNATE_RESERVE: ("target_fen",),
    _K.CONFIRM_BILL: ("commitment_id",),
    _K.CANCEL_BILL: ("commitment_id",),
    _K.MARK_PAID_EXTERNAL: ("commitment_id",),
    _K.RESOLVE_ITEM: ("account_id", "txn_id", "category"),
}

# What each bill action does to the commitment.
BILL_TARGET_STATUS: Mapping[ActionKind, CommitmentStatus] = {
    _K.CONFIRM_BILL: CommitmentStatus.CONFIRMED,
    _K.CANCEL_BILL: CommitmentStatus.CANCELLED,
    _K.MARK_PAID_EXTERNAL: CommitmentStatus.PAID_REPORTED,
}
BILL_CHANGE: Mapping[ActionKind, CommitmentChange] = {
    _K.CONFIRM_BILL: CommitmentChange.CONFIRMED,
    _K.CANCEL_BILL: CommitmentChange.CANCELLED,
    _K.MARK_PAID_EXTERNAL: CommitmentChange.PAID_REPORTED,
}
BILL_FROM_STATUSES: Mapping[ActionKind, frozenset[CommitmentStatus]] = {
    _K.CONFIRM_BILL: frozenset({CommitmentStatus.CANDIDATE}),
    _K.CANCEL_BILL: commitments.OWED,
    _K.MARK_PAID_EXTERNAL: commitments.OWED,
}


class BillsPack:
    kinds = frozenset(PARAMS)

    def canonical(self, kind: ActionKind, params: Mapping[str, Any]) -> dict[str, Any]:
        required = PARAMS[ActionKind(kind)]
        unknown = set(params) - set(required)
        missing = [name for name in required if params.get(name) is None]
        if unknown or missing:
            raise ValueError(f"{kind} params: unknown {sorted(unknown)}, missing {missing}")
        result = {name: params[name] for name in required}
        if "target_fen" in result and (
            type(result["target_fen"]) is not int or result["target_fen"] < 0
        ):
            raise ValueError("target_fen is a non-negative integer")
        return result

    def request_hash(self, kind: ActionKind, customer_id: str, params: Mapping[str, Any]) -> str:
        business: dict[str, Any] = {"kind": ActionKind(kind).value, "customer_id": customer_id}
        if "target_fen" in params:
            business.update(counterparty="reserve:emergency", amount_fen=params["target_fen"])
        if "commitment_id" in params:
            business["commitment_id"] = params["commitment_id"]
        if "txn_id" in params:
            business.update(
                account_id=params["account_id"],
                counterparty=params["txn_id"],
                purpose=params["category"],
            )
        return sha256_hex(business)

    def tier(
        self,
        reader: Reader,
        customer_id: str,
        kind: ActionKind,
        params: Mapping[str, Any],
        at: datetime,
    ) -> Tier:
        if ActionKind(kind) is ActionKind.DESIGNATE_RESERVE:
            delta = int(params["target_fen"]) - reserves.reserve_target(reader, customer_id)
            return k3_policy.tier_for(kind, reserve_delta_fen=delta)
        return k3_policy.tier_for(kind)

    def precheck(
        self,
        reader: Reader,
        customer_id: str,
        kind: ActionKind,
        params: Mapping[str, Any],
        at: datetime,
    ) -> tuple[Infeasible, ...]:
        kind = ActionKind(kind)
        if kind is ActionKind.DESIGNATE_RESERVE:
            return ()
        if kind is ActionKind.RESOLVE_ITEM:
            return _resolve_refusals(reader, customer_id, params)
        bill = _owned_bill(reader, customer_id, str(params["commitment_id"]))
        if bill.status not in BILL_FROM_STATUSES[kind]:
            return (Infeasible(ErrorCode.BILL_NOT_PAYABLE, {"status": bill.status.value}),)
        if kind is not ActionKind.CONFIRM_BILL and commitments.open_payment_fen(
            reader, bill.commitment_id
        ):
            # A payment in flight would settle against a bill that no longer protects money.
            return (Infeasible(ErrorCode.BILL_NOT_PAYABLE, {"reason": "payment_in_flight"}),)
        return ()

    def card(self, reader: Reader, action: ActionRow, at: datetime) -> CardContent:
        if action.kind is ActionKind.DESIGNATE_RESERVE:
            return self._reserve_card(reader, action, at)
        if action.kind is ActionKind.RESOLVE_ITEM:
            return self._resolve_card(reader, action)
        return self._bill_card(reader, action, at)

    def execute(self, uow: UnitOfWork, action: ActionRow, at: datetime) -> None:
        kind = action.kind
        if kind is ActionKind.DESIGNATE_RESERVE:
            reserves.set_reserve_target(uow, action.customer_id, int(action.params["target_fen"]))
        elif kind is ActionKind.RESOLVE_ITEM:
            params = action.params
            imports.resolve_line(
                uow, str(params["account_id"]), str(params["txn_id"]), str(params["category"])
            )
        else:
            self._change_bill(uow, action)

    def _change_bill(self, uow: UnitOfWork, action: ActionRow) -> None:
        bill = _owned_bill(uow, action.customer_id, str(action.params["commitment_id"]))
        delta = bill.unpaid_fen if action.kind is ActionKind.CONFIRM_BILL else -bill.unpaid_fen
        commitments.transition(
            uow,
            bill,
            to=BILL_TARGET_STATUS[action.kind],
            change=BILL_CHANGE[action.kind],
            delta_fen=delta,
            source_ref=action.logical_action_id,
        )

    # Cards

    def _reserve_card(self, reader: Reader, action: ActionRow, at: datetime) -> CardContent:
        target = int(action.params["target_fen"])
        inputs, before = _values(reader, action.customer_id, at)
        after = compute_snapshot(inputs.model_copy(update={"reserve_fen": target}))
        warnings = ["这笔设置不会产生新的资金，只改变受保护的金额。"]
        if target < inputs.reserve_fen:
            warnings.append("下调后受保护金额减少、可自由支配金额增加，确认后需要输入短信验证码。")
        return CardContent(
            title="调整应急储备",
            lines=(
                CardLine(key="reserve_before", label="当前储备", text=fen_text(inputs.reserve_fen)),
                CardLine(key="reserve_after", label="调整后储备", text=fen_text(target)),
            ),
            changes=_protection_changes(before, after),
            warnings=tuple(warnings),
        )

    def _bill_card(self, reader: Reader, action: ActionRow, at: datetime) -> CardContent:
        bill = _owned_bill(reader, action.customer_id, str(action.params["commitment_id"]))
        status = BILL_TARGET_STATUS[action.kind]
        inputs, before = _values(reader, action.customer_id, at)
        after = compute_snapshot(_with_status(inputs, bill.commitment_id, status))
        payee = accounts.find_payee(reader, bill.payee_id)
        assert payee is not None
        lines = [
            CardLine(key="bill", label="账单", text=bill_label(bill.kind)),
            CardLine(
                key="payee",
                label="收款人",
                text=payee_handle(payee.alias, payee.bank, payee.account_masked),
            ),
            CardLine(key="amount", label="金额", text=fen_text(bill.amount_fen)),
            CardLine(key="unpaid", label="待付", text=fen_text(bill.unpaid_fen)),
            CardLine(key="due", label="到期日", text=bill.due_date.isoformat()),
        ]
        warnings = _bill_warnings(reader, action.kind, bill)
        titles = {
            ActionKind.CONFIRM_BILL: "确认账单",
            ActionKind.CANCEL_BILL: "取消账单",
            ActionKind.MARK_PAID_EXTERNAL: "标记为已在他处支付",
        }
        return CardContent(
            title=f"{titles[action.kind]}：{bill_label(bill.kind)}",
            lines=tuple(lines),
            changes=_protection_changes(before, after),
            sources=bill_sources(bill),
            warnings=warnings,
        )

    def _resolve_card(self, reader: Reader, action: ActionRow) -> CardContent:
        line = imports.find_line(reader, action.customer_id, str(action.params["txn_id"]))
        assert line is not None
        category = str(action.params["category"])
        return CardContent(
            title="归类这笔交易",
            lines=(
                CardLine(key="posted_on", label="日期", text=line.posted_on.isoformat()),
                CardLine(key="amount", label="金额", text=fen_text(line.amount_fen)),
                CardLine(
                    key="counterparty", label="对方（银行流水原文）", text=line.counterparty_raw
                ),
                CardLine(
                    key="description", label="摘要（银行流水原文）", text=line.description_raw
                ),
                CardLine(key="category", label="归类为", text=CATEGORIES[category]),
            ),
            sources=(
                SourceLabel(
                    slot="流水文字",
                    provenance=Provenance.UNTRUSTED_TEXT,
                    label=SOURCE_LABELS[Provenance.UNTRUSTED_TEXT],
                ),
                SourceLabel(
                    slot="类别",
                    provenance=Provenance.USER_UTTERANCE,
                    label=SOURCE_LABELS[Provenance.USER_UTTERANCE],
                ),
            ),
            warnings=("归类只影响收入和支出的统计，不改变受保护的金额。",),
        )


def _values(
    reader: Reader, customer_id: str, at: datetime
) -> tuple[CashInputs, CashSnapshotValues]:
    inputs = cash_inputs_from_db(reader, customer_id, at)
    return inputs, compute_snapshot(inputs)


def _with_status(inputs: CashInputs, commitment_id: str, status: CommitmentStatus) -> CashInputs:
    changed = tuple(
        c.model_copy(update={"status": status}) if c.commitment_id == commitment_id else c
        for c in inputs.commitments
    )
    return inputs.model_copy(update={"commitments": changed})


def _protection_changes(
    before: CashSnapshotValues, after: CashSnapshotValues
) -> tuple[MoneyChange, ...]:
    return (
        MoneyChange(
            key="protected",
            label="受保护金额",
            before=before.protected_fen,
            after=after.protected_fen,
        ),
        MoneyChange(
            key="headroom", label="可自由支配", before=before.headroom_fen, after=after.headroom_fen
        ),
    )


def _bill_warnings(reader: Reader, kind: ActionKind, bill: CommitmentRow) -> tuple[str, ...]:
    if kind is ActionKind.CONFIRM_BILL:
        return ("确认后，这笔账单的待付金额计入受保护金额；确认不会付款。",)
    warnings = ["受保护金额减少、可自由支配金额增加，确认后需要输入短信验证码。"]
    if kind is ActionKind.CANCEL_BILL:
        warnings.append("取消不会退回任何款项，只是不再为这笔账单预留资金。")
        if _excluded_from_budget(reader, bill):
            warnings.append("这笔账单已从日常预算中排除，取消后它既不在预算内，也不再受保护。")
    else:
        warnings.append("没有银行回单：状态为“据您所述已支付”，并作为假设显示在计算明细中。")
    return tuple(warnings)


def _excluded_from_budget(reader: Reader, bill: CommitmentRow) -> bool:
    rows = reader.execute(
        "SELECT excluded_purpose_ids FROM allowances WHERE customer_id = ?", (bill.customer_id,)
    ).fetchall()
    return any(bill.purpose_id in json.loads(row["excluded_purpose_ids"]) for row in rows)


def bill_sources(bill: CommitmentRow) -> tuple[SourceLabel, ...]:
    provenance = BILL_SOURCE_PROVENANCE.get(bill.source, Provenance.UNTRUSTED_TEXT)
    label = SOURCE_LABELS[provenance]
    return tuple(
        SourceLabel(slot=slot, provenance=provenance, label=label)
        for slot in ("账单", "金额", "到期日")
    )


def _owned_bill(reader: Reader, customer_id: str, commitment_id: str) -> CommitmentRow:
    bill = commitments.find_commitment(reader, commitment_id)
    if bill is None or bill.customer_id != customer_id:
        raise ValueError(f"commitment {commitment_id} is not the customer's")
    return bill


def _resolve_refusals(
    reader: Reader, customer_id: str, params: Mapping[str, Any]
) -> tuple[Infeasible, ...]:
    line = imports.find_line(reader, customer_id, str(params["txn_id"]))
    if line is None or line.account_id != params["account_id"]:
        raise ValueError(f"line {params['txn_id']} is not on the customer's account")
    if params["category"] not in CATEGORIES:
        raise ValueError(f"unknown category {params['category']}")
    if line.resolved:
        return (Infeasible(ErrorCode.ALREADY_SETTLED, {"txn_id": line.source_txn_id}),)
    return ()
