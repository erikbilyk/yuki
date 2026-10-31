"""K7: the facts behind a reply and the check of the model's explanation (SPEC §17).

Facts come from the results of the turn's tool calls, which the services built from snapshots,
receipts, calc results, checkpoints and plan rows; draft amounts are read back from the stored
action. The fact block is rendered from fixed zh-CN templates. An explanation with any claim
the facts do not support is replaced by a template and the replacement is audited. A money or
percent claim that only restates a value the customer wrote is not held against the reply, and a
clarifying question loses only its unsupported asides when the rest stands on its own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel

from wg.contracts.api import (
    AccountsView,
    ApprovalRequestResult,
    BalanceView,
    BillsView,
    CancelResult,
    CashFields,
    CashSnapshotView,
    DebitRequestsView,
    DebitRequestView,
    DraftResult,
    ExposureView,
    FeeEstimateView,
    MandatesView,
    MandateView,
    PlanDraftResult,
    PlanView,
    PositionsView,
    StressView,
    SubscriptionSpendView,
    TransferHistoryView,
    UnresolvedView,
)
from wg.contracts.engines import ExposureGroup
from wg.contracts.enums import (
    ActionKind,
    ActionStatus,
    AuditActor,
    BillStatus,
    CheckpointResult,
    CommitmentStatus,
    DebitReason,
    DebitRequestStatus,
    EffectCheck,
    MandateStatus,
    OperationStatus,
    PlanStatus,
)
from wg.contracts.errors import ErrorCode
from wg.contracts.facts import (
    ClaimViolation,
    ClaimViolationReason,
    CountFact,
    DateFact,
    Fact,
    FactBlock,
    FactConcept,
    FactKind,
    MoneyFact,
    PercentFact,
    StatusFact,
)
from wg.contracts.tools import (
    CandidateBrief,
    CandidateSummary,
    OperationStatusResult,
    PlanStatusResult,
)
from wg.core import k5_continuation, mandate_rules
from wg.core.k2_context import Utterance
from wg.k6 import accounts, actions, audit, commitments, funds, mandates, plans
from wg.k6.db import Database, Reader
from wg.k6.simtime import SHANGHAI
from wg.packs.labels import bill_label
from wg.textcheck.amounts import (
    GE,
    GT,
    LE,
    LT,
    amounts_outside,
    find_amounts,
    parse_chinese_numeral,
)
from wg.textcheck.claims import FEASIBLE_WITH_APPROVAL, check_claims

REPLACED_EVENT = "explanation_replaced"
# Added to the audited reasons when unsupported asides were cut from a clarifying question.
TRIMMED_DECISION = "trimmed"
# A purchase paused by these is still possible at the new amount once the customer approves it.
BOUND_PAUSES = frozenset(
    {
        ErrorCode.BILL_INCREASE_OVER_LIMIT,
        ErrorCode.RETENTION_OVER_LIMIT,
        ErrorCode.PURCHASE_BELOW_MIN,
    }
)
# With these the new amount does not fit the money there is, whatever the approval.
UNFUNDED_PAUSES = frozenset({ErrorCode.INSUFFICIENT_HEADROOM, ErrorCode.EXACT_ORDERS_ONLY})
_C = FactConcept

AMOUNT_CONCEPT = {
    ActionKind.REDEEM: _C.REDEMPTION_GROSS,
    ActionKind.PURCHASE: _C.PURCHASE_AMOUNT,
}

KIND_TEXT: dict[ActionKind, str] = {
    ActionKind.TRANSFER: "转账",
    ActionKind.PAY_BILL: "账单付款",
    ActionKind.DESIGNATE_RESERVE: "预留调整",
    ActionKind.CONFIRM_BILL: "确认账单",
    ActionKind.RESOLVE_ITEM: "交易分类",
    ActionKind.CANCEL_BILL: "取消账单",
    ActionKind.MARK_PAID_EXTERNAL: "标记为已在他处支付",
    ActionKind.REDEEM: "赎回",
    ActionKind.PURCHASE: "买入",
    ActionKind.CHECK_OUTCOME: "结果检查",
    ActionKind.ADD_PAYEE: "新增收款人",
    ActionKind.MANDATE_CREATE: "新增自动扣款",
}
# Direct-debit actions are named after the merchant of their agreement.
MANDATE_KIND_TEXT: dict[ActionKind, str] = {
    ActionKind.DEBIT_PAY: "扣款付款",
    ActionKind.DEBIT_DECLINE: "拒绝扣款",
    ActionKind.MANDATE_CANCEL: "关闭自动扣款",
    ActionKind.MANDATE_SUSPEND: "暂停自动扣款",
    ActionKind.MANDATE_RESUME: "恢复自动扣款",
    ActionKind.MANDATE_CAP_LOWER: "调整扣款上限",
    ActionKind.MANDATE_CAP_RAISE: "调整扣款上限",
}

# Status facts are labelled with the bare name of their subject, because the claim checker
# ties a status word to a subject through the words of its labels.
STATUS_TEXT: dict[str, str] = {
    ActionStatus.DRAFT: "草稿，尚未提交确认",
    ActionStatus.AWAITING_APPROVAL: "等待您在确认卡片上确认",
    ActionStatus.AWAITING_STEPUP: "等待短信验证码",
    ActionStatus.READY: "已确认，等待执行",
    ActionStatus.SUBMITTED: "已提交，银行处理中",
    ActionStatus.SETTLED: "已完成",
    ActionStatus.REJECTED: "银行未受理，资金未转出",
    ActionStatus.CANCELLED: "已取消",
    ActionStatus.UNKNOWN: "执行结果待核实",
    ActionStatus.PAUSED: "已暂停",
    ActionStatus.SUPERSEDED: "已被新版本取代",
    PlanStatus.DRAFT: "待确认",
    PlanStatus.ACTIVE: "进行中",
    PlanStatus.PAUSED: "已暂停",
    PlanStatus.LOCKED: "已锁定，等待人工核实",
    PlanStatus.COMPLETED: "全部完成",
    PlanStatus.PARTIAL: "部分完成",
    CheckpointResult.PASSED: "达到目标",
    CheckpointResult.FAILED: "未达到目标",
    CheckpointResult.CHECKPOINT_MISSED: "错过检查时间",
    BillStatus.SECURED: "资金已保障，尚未支付",
    BillStatus.UNSECURED: "资金未保障，尚未支付",
    BillStatus.OVERDUE: "已逾期，尚未支付",
    BillStatus.PAID: "已付清",
    BillStatus.PAID_REPORTED: "据您所述已在他处支付，未经银行核实",
    CommitmentStatus.CANDIDATE: "待您确认的账单",
    CommitmentStatus.CONFIRMED: "已确认，尚未支付",
    CommitmentStatus.PARTIALLY_PAID: "部分已付",
    CommitmentStatus.PAID: "已付清",
    CommitmentStatus.CANCELLED: "已取消",
    CommitmentStatus.DISMISSED: "已忽略",
    FEASIBLE_WITH_APPROVAL: "可行，需要您重新确认",
}

# Words for the facts of direct-debit agreements. An agreement's status speaks of the debit
# authorisation, never of the service contract, which the bank does not change.
CONCEPT_STATUS_TEXT: dict[FactConcept, dict[str, str]] = {
    FactConcept.MANDATE_STATUS: {
        MandateStatus.ACTIVE: "扣款授权有效",
        MandateStatus.SUSPENDED: "扣款授权已暂停",
        MandateStatus.CANCELLED: "扣款授权已解除",
        MandateStatus.EXPIRED: "扣款授权已到期",
    },
    FactConcept.DEBIT_REQUEST_STATUS: {
        DebitRequestStatus.AWAITING_CLIENT: "等待您处理",
        DebitRequestStatus.PAID: "已按您的确认扣款",
        DebitRequestStatus.DECLINED: "已被您拒绝",
        DebitRequestStatus.EXPIRED: "超过处理期限，已作废",
        DebitRequestStatus.AUTO_DECLINED: "已被系统自动拒绝",
        DebitRequestStatus.OUT_OF_MANDATE: "超出扣款授权，已被拒绝",
    },
    FactConcept.DEBIT_REQUEST_REASON: {
        DebitReason.NO_MANDATE: "没有对应的扣款授权",
        DebitReason.MANDATE_SUSPENDED: "扣款授权已暂停",
        DebitReason.MANDATE_CANCELLED: "扣款授权已解除",
        DebitReason.MANDATE_EXPIRED: "扣款授权已到期",
        DebitReason.CAP: "超过扣款上限",
        DebitReason.PERIOD_USED: "本期已扣款",
        DebitReason.EXPIRED: "超过处理期限",
    },
    FactConcept.EFFECT_CHECK: {
        EffectCheck.VERIFIED: "已核实，银行拒绝了新的扣款试探",
        EffectCheck.PENDING: "已保存，正在核实",
        EffectCheck.FAILED: "核实未通过，需要人工核实",
    },
}

TEMPLATE_AWAITING = "已为您准备好确认卡片，请在卡片上核对后自行确认；上方数字以系统记录为准。"
TEMPLATE_DRAFT = "已生成草稿，尚未提交确认；上方数字以系统记录为准。"
TEMPLATE_FACTS = "以上数字和状态来自系统记录，请以此为准。"
TEMPLATE_EMPTY = "抱歉，我暂时无法给出可靠的说明，请换一种方式描述您的需求。"

_R = ClaimViolationReason
# Mismatch reasons a restated value may carry only with the customer's own comparator before it
# ("手续费不超过20元" against a fee fact of 15 yuan restates the customer's limit).
COMPARATOR_REASONS = frozenset({_R.VALUE_MISMATCH, _R.CONCEPT_MISMATCH, _R.PRECISION})
# A sentence with one of these speaks about what the account holds, so a value in it is a claim
# about the account even when the customer wrote the same number.
ACCOUNT_STATE_WORDS = (
    "余额",
    "账上",
    "账户里",
    "账户中",
    "共有",
    "还有",
    "剩余",
    "剩下",
    "到账",
    "可用",
    "现金",
    "存款",
    "结余",
    "可支配",
    "自由支配",
    "缺口",
    "持有",
    "市值",
)
# A sentence with one of these says what was done, is under way or is (not) possible; a value in
# it is a claim about an outcome ("已为您买入4,785元", "买入4,785元可以执行").
OUTCOME_WORDS = (
    "已",
    "处理中",
    "执行中",
    "进行中",
    "成功",
    "可执行",
    "可以执行",
    "能执行",
    "可行",
    "够",
    "可以转",
    "可以买",
    "可以付",
    "能转",
    "能买",
    "能付",
    "不可能",
    "无法",
    "不能",
    "做不到",
    "办不到",
)
FEE_WORDS = ("手续费", "赎回费", "费用", "费率")
_NO_FACTS = FactBlock(facts=())
_SENTENCE_END_RE = re.compile(r"[。！？!?；;\n]")
# Parsed as the claim checker parses percents, so a reply's percent and a customer's compare.
_PERCENT_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*[%％]|百分之([零〇一二两三四五六七八九十百]+|\d+(?:\.\d+)?)"
)
_PERCENT_COMPARATOR_WORDS = {
    "不超过": LE,
    "不高于": LE,
    "不大于": LE,
    "最多": LE,
    "至多": LE,
    "≤": LE,
    "<=": LE,
    "低于": LT,
    "少于": LT,
    "小于": LT,
    "不到": LT,
    "<": LT,
    "＜": LT,
    "不低于": GE,
    "不少于": GE,
    "不小于": GE,
    "至少": GE,
    "最少": GE,
    "≥": GE,
    ">=": GE,
    "高于": GT,
    "多于": GT,
    "超过": GT,
    "大于": GT,
    ">": GT,
    "＞": GT,
}
_PERCENT_COMPARATOR_BEFORE_RE = re.compile(
    "("
    + "|".join(re.escape(w) for w in sorted(_PERCENT_COMPARATOR_WORDS, key=len, reverse=True))
    + r")\s*(?:为|是)?\s*$"
)
_PERCENT_COMPARATOR_AFTER_RE = re.compile(r"\s*(及以下|以下|以内|及以上|以上)")
_PERCENT_COMPARATOR_AFTER = {"及以下": LE, "以下": LE, "以内": LE, "及以上": GE, "以上": GE}


@dataclass(frozen=True)
class VerifiedReply:
    """The fact block and the explanation the customer may see."""

    facts: FactBlock
    fact_lines: tuple[str, ...]
    explanation: str | None
    replaced: bool
    violations: tuple[ClaimViolation, ...] = ()


class FactCollector:
    """Turns service results into typed facts; the same fact is kept once."""

    def __init__(self, reader: Reader, as_of: datetime) -> None:
        self.reader = reader
        self.as_of = as_of
        self._facts: list[Fact] = []
        self._seen: set[tuple[Any, ...]] = set()

    def block(self) -> FactBlock:
        return FactBlock(facts=tuple(self._facts))

    def add(self, result: BaseModel) -> None:
        match result:
            case CashFields():
                self._cash(result)
            case AccountsView():
                for account in result.accounts:
                    self._balance(account.account_id, account.name, account.available_fen)
            case BalanceView():
                self._balance(result.account_id, result.name, result.available_fen, result.as_of)
            case BillsView():
                for bill in result.bills:
                    self._bill(
                        result.snapshot_id,
                        bill.commitment_id,
                        bill_label(bill.kind),
                        bill.unpaid_fen,
                        bill.derived.value if bill.derived else bill.status.value,
                        amount_fen=bill.amount_fen,
                        due_date=bill.due_date,
                    )
            case UnresolvedView():
                self._count(_C.UNRESOLVED_COUNT, result.count, "未分类交易笔数", result.snapshot_id)
            case TransferHistoryView():
                for transfer in result.transfers:
                    self._operation(
                        transfer.logical_action_id,
                        transfer.kind,
                        transfer.amount_fen,
                        transfer.status,
                        f"转给{transfer.payee_display}",
                        transfer.receipt_id or transfer.operation_id,
                        subject=transfer.commitment_id or transfer.logical_action_id,
                    )
            case OperationStatusResult():
                self._operation_status(result)
            case DraftResult() | ApprovalRequestResult() | CancelResult():
                self._action(result.logical_action_id, result.status)
            case PlanDraftResult():
                for draft in result.actions:
                    self._action(draft.logical_action_id, draft.status)
                if result.plan_id is not None:
                    pid = result.plan_id
                    self._status(_C.PLAN_STATUS, PlanStatus.DRAFT, "计划", pid, pid)
            case PlanView():
                self._plan(result)
            case CandidateSummary():
                self._candidates(result)
            case ExposureView():
                self._exposure(result)
            case PositionsView():
                for position in result.positions:
                    self._money(
                        _C.EXPOSURE_VALUE,
                        position.value_fen,
                        f"{position.name}市值",
                        position.product_id,
                        subject=position.product_id,
                        as_of=result.as_of,
                    )
                self._money(_C.EXPOSURE_VALUE, result.total_fen, "基金市值合计", "positions")
            case FeeEstimateView():
                self._fee_quote(result)
            case StressView():
                self._money(_C.FEE, result.result.fee_fen, "调仓手续费", result.calc_result_id)
                for row in result.result.rows:
                    ref = result.calc_result_id
                    self._money(_C.STRESS_LOSS, row.before_loss_fen, f"{row.shock}调仓前损失", ref)
                    self._money(_C.STRESS_LOSS, row.after_loss_fen, f"{row.shock}调仓后损失", ref)
            case PlanStatusResult():
                for plan in result.plans:
                    self._plan(plan)
            case MandatesView():
                self._mandates(result)
            case DebitRequestsView():
                for request in result.requests:
                    self._debit_request(request)
            case SubscriptionSpendView():
                self._subscription_spend(result)

    # Builders

    def _cash(self, cash: CashFields) -> None:
        ref, at = cash.snapshot_id, cash.as_of
        if cash.shortfall_fen > 0:
            self._money(_C.SHORTFALL, cash.shortfall_fen, "缺口", ref, as_of=at)
        else:
            self._money(_C.HEADROOM, cash.headroom_fen, "可自由支配", ref, as_of=at)
        self._money(_C.PROTECTED, cash.protected_fen, "受保护资金", ref, as_of=at)
        self._money(_C.BILL_UNPAID, cash.unpaid_bills_fen, "未支付账单合计", ref, as_of=at)
        self._count(_C.BILL_UNPAID, cash.unpaid_bills_count, "未支付账单笔数", ref, as_of=at)
        if cash.next_operation is not None and cash.next_operation.amount_fen is not None:
            nxt = cash.next_operation
            self._money(
                AMOUNT_CONCEPT.get(nxt.kind, _C.OPERATION_AMOUNT),
                nxt.amount_fen,
                "下一笔操作金额",
                ref,
                subject=nxt.logical_action_id,
                as_of=at,
            )
        if isinstance(cash, CashSnapshotView):
            details = cash.details
            self._money(_C.POSTED_CASH, details.c_fen, "账户现金合计", ref, as_of=at)
            estimate = details.monthly_estimate_fen
            label = "本月结余估算（不是可自由支配）"
            self._money(_C.MONTHLY_ESTIMATE, estimate, label, ref, as_of=at)
            for bill in details.bills:
                self._bill(
                    ref,
                    bill.commitment_id,
                    self._bill_name(bill.commitment_id),
                    bill.unpaid_fen,
                    bill.status.value,
                    due_date=bill.due_date,
                    as_of=at,
                )
            for period in details.subscriptions:
                self._bill(
                    ref,
                    mandate_rules.subscription_commitment_id(period.mandate_ref, period.period_key),
                    f"{period.merchant_display}自动扣款",
                    period.expected_fen,
                    period.status.value,
                    due_date=period.due_date,
                    as_of=at,
                )

    def _balance(
        self, account_id: str, name: str, available_fen: int, as_of: datetime | None = None
    ) -> None:
        self._money(
            _C.ACCOUNT_BALANCE,
            available_fen,
            f"{name}可用余额",
            account_id,
            subject=account_id,
            as_of=as_of,
        )

    def _bill(
        self,
        ref: str,
        commitment_id: str,
        name: str,
        unpaid_fen: int,
        status: str,
        *,
        amount_fen: int | None = None,
        due_date: date | None = None,
        as_of: datetime | None = None,
    ) -> None:
        if amount_fen is not None:
            label = f"{name}账单金额"
            self._money(_C.BILL_AMOUNT, amount_fen, label, ref, subject=commitment_id, as_of=as_of)
        label = f"{name}未付金额"
        self._money(_C.BILL_UNPAID, unpaid_fen, label, ref, subject=commitment_id, as_of=as_of)
        self._status(_C.BILL_STATUS, status, name, ref, commitment_id, as_of=as_of)
        if due_date is not None:
            self._add(
                DateFact,
                _C.DUE_DATE,
                due_date,
                f"{name}到期日",
                ref,
                subject=commitment_id,
                as_of=as_of,
            )

    def _operation(
        self,
        logical_action_id: str,
        kind: ActionKind,
        amount_fen: int | None,
        status: OperationStatus,
        label: str,
        ref: str,
        *,
        subject: str,
        receipt: bool = True,
    ) -> None:
        if amount_fen is not None:
            concept = AMOUNT_CONCEPT.get(kind, _C.OPERATION_AMOUNT)
            self._money(concept, amount_fen, f"{label}金额", ref, subject=logical_action_id)
        if receipt and status in (OperationStatus.SETTLED, OperationStatus.REJECTED):
            self._status(_C.RECEIPT_STATUS, status.value, label, ref, subject)
        else:
            self._status(_C.ACTION_STATUS, status.value, label, ref, logical_action_id)

    def _operation_status(self, result: OperationStatusResult) -> None:
        lid = result.logical_action_id
        action = actions.find_current(self.reader, lid)
        label = self._action_label(action) if action else "该操作"
        self._status(_C.ACTION_STATUS, result.action_status.value, label, lid, lid)
        receipt, operation = result.receipt, result.operation
        if receipt is not None:
            ref = receipt.receipt_id
            subject = receipt.commitment_id or lid
            self._status(_C.RECEIPT_STATUS, receipt.status.value, label, ref, subject)
            concept = AMOUNT_CONCEPT.get(receipt.kind, _C.OPERATION_AMOUNT)
            for fact_concept, value, suffix in (
                (concept, receipt.amount_fen, "金额"),
                (_C.FEE, receipt.fee_fen, "手续费"),
                (_C.REDEMPTION_NET, receipt.net_fen, "到账金额"),
            ):
                if value is not None:
                    self._money(fact_concept, value, f"{label}{suffix}", ref, subject=lid)
            if receipt.effect_check is not None and receipt.mandate_ref is not None:
                self._status(
                    _C.EFFECT_CHECK,
                    receipt.effect_check.value,
                    receipt.merchant_display or "该商户",
                    ref,
                    receipt.mandate_ref,
                )
            if receipt.unpaid_after_fen is not None and receipt.commitment_id is not None:
                self._money(
                    _C.BILL_UNPAID,
                    receipt.unpaid_after_fen,
                    f"{self._bill_name(receipt.commitment_id)}未付金额",
                    ref,
                    subject=receipt.commitment_id,
                )
        elif operation is not None and operation.amount_fen is not None:
            concept = AMOUNT_CONCEPT.get(operation.kind, _C.OPERATION_AMOUNT)
            ref = operation.operation_id
            self._money(concept, operation.amount_fen, f"{label}金额", ref, subject=lid)

    def _action(self, logical_action_id: str, status: ActionStatus) -> None:
        """A drafted or carded action: its status, and its amount as stored in the database."""
        action = actions.find_current(self.reader, logical_action_id)
        if action is None:
            return
        label = self._action_label(action)
        lid = logical_action_id
        self._status(_C.ACTION_STATUS, status.value, label, lid, lid)
        amount = action.params.get("amount_fen")
        if isinstance(amount, int):
            concept = AMOUNT_CONCEPT.get(action.kind, _C.OPERATION_AMOUNT)
            self._money(concept, amount, f"{label}金额", lid, subject=lid)
        cap = action.params.get("cap_fen")
        if isinstance(cap, int):
            self._money(_C.MANDATE_CAP, cap, f"{label}后的扣款上限", lid, subject=lid)
        target = action.params.get("target_fen")
        if action.kind is ActionKind.DESIGNATE_RESERVE and isinstance(target, int):
            self._money(_C.RESERVE_TARGET, target, "预留目标", lid, subject=lid)

    def _plan(self, plan: PlanView) -> None:
        pid = plan.plan_id
        self._status(_C.PLAN_STATUS, plan.status.value, "计划", pid, pid)
        for node in plan.nodes:
            lid = node.logical_action_id
            label = f"转给{node.payee_display}" if node.payee_display else KIND_TEXT[node.kind]
            self._status(_C.ACTION_STATUS, node.status.value, label, pid, lid)
            if node.amount_fen is not None:
                concept = AMOUNT_CONCEPT.get(node.kind, _C.OPERATION_AMOUNT)
                self._money(concept, node.amount_fen, f"{label}金额", pid, subject=lid)
            if node.kind is ActionKind.PURCHASE and node.status is ActionStatus.PAUSED:
                self._paused_purchase(pid, lid)
        for revision in plan.revisions:
            subject = f"{pid}#{revision.revision}"
            if revision.purchase_after_fen is not None:
                self._money(
                    _C.PURCHASE_AMOUNT,
                    revision.purchase_after_fen,
                    f"第{revision.revision}版买入金额",
                    pid,
                    subject=subject,
                )
                if not revision.within_bounds:
                    self._status(
                        _C.OPTION_FEASIBILITY,
                        FEASIBLE_WITH_APPROVAL,
                        f"第{revision.revision}版买入",
                        pid,
                        subject,
                    )
        checkpoint = plan.checkpoint
        if checkpoint is not None:
            ref = checkpoint.logical_action_id
            at = checkpoint.evaluated_at
            self._status(
                _C.CHECKPOINT_RESULT, checkpoint.result.value, "检查点结果", ref, pid, as_of=at
            )
            self._percent(_C.SECTOR_SHARE, checkpoint.measured_pct, "检查点占比", ref, as_of=at)
            self._percent(_C.TARGET_MAX, checkpoint.target_max_pct, "目标上限", ref, as_of=at)
        if plan.cash is not None:
            self._cash(plan.cash)

    def _mandates(self, view: MandatesView) -> None:
        self._money(
            _C.SUBSCRIPTION_MONTHLY_TOTAL,
            view.monthly_total_fen,
            "每月自动扣款合计",
            "mandates",
            as_of=view.as_of,
        )
        for mandate in view.mandates:
            self._mandate(mandate, view.as_of)

    def _mandate(self, mandate: MandateView, as_of: datetime) -> None:
        name, ref, subject = mandate.merchant_display, mandate.mandate_ref, mandate.mandate_ref
        self._status(_C.MANDATE_STATUS, mandate.status.value, name, ref, subject, as_of=as_of)
        self._money(_C.MANDATE_CAP, mandate.cap_fen, f"{name}扣款上限", ref, subject=subject)
        if mandate.status is MandateStatus.ACTIVE:
            label = f"{name}每月扣款金额"
            self._money(_C.MANDATE_EXPECTED, mandate.expected_fen, label, ref, subject=subject)
            if mandate.next_debit_date is not None:
                label = f"{name}下次扣款日"
                self._add(
                    DateFact, _C.DUE_DATE, mandate.next_debit_date, label, ref, subject=subject
                )

    def _debit_request(self, request: DebitRequestView) -> None:
        name, ref, subject = request.merchant_display, request.request_id, request.request_id
        self._status(_C.DEBIT_REQUEST_STATUS, request.status.value, name, ref, subject)
        self._money(
            _C.DEBIT_REQUEST_AMOUNT, request.amount_fen, f"{name}扣款请求金额", ref, subject=subject
        )
        if request.reason_code is not None:
            self._status(
                _C.DEBIT_REQUEST_REASON, request.reason_code.value, f"{name}扣款请求", ref, subject
            )
        if request.status is DebitRequestStatus.AWAITING_CLIENT:
            self._add(
                DateFact,
                _C.RESPOND_BY,
                request.respond_by.astimezone(SHANGHAI).date(),
                f"{name}扣款请求处理截止日",
                ref,
                subject=subject,
            )

    def _subscription_spend(self, view: SubscriptionSpendView) -> None:
        ref = f"subscription_spend:{view.days}"
        for row in view.rows:
            name, subject = row.merchant_display, row.mandate_ref
            label = f"{name}近{view.days}天已扣款"
            self._money(_C.SUBSCRIPTION_PAID, row.paid_fen, label, ref, subject=subject)
            if row.last_amount_fen is not None:
                label = f"{name}最近一次扣款"
                self._money(_C.SUBSCRIPTION_PAID, row.last_amount_fen, label, ref, subject=subject)
        self._money(_C.SUBSCRIPTION_PAID, view.total_fen, f"近{view.days}天自动扣款合计", ref)

    def _paused_purchase(self, plan_id: str, logical_action_id: str) -> None:
        """The amount a purchase paused outside its bounds could buy with a new approval (T47)."""
        plan = plans.find_plan(self.reader, plan_id)
        node = actions.find_current(self.reader, logical_action_id)
        if plan is None or node is None or plan.root_approval_id is None:
            return
        if not BOUND_PAUSES & {ErrorCode(r) for r in node.pause_reasons}:
            return
        decision = k5_continuation.decide(self.reader, plan, node, self.as_of)
        product = funds.get_product(self.reader, str(node.params["product_id"]))
        fits = not UNFUNDED_PAUSES & set(decision.reasons)
        if not fits or decision.purchase_fen < max(product.min_purchase_fen, 1):
            return
        subject = f"{logical_action_id}#new_approval"
        self._money(
            _C.PURCHASE_AMOUNT,
            decision.purchase_fen,
            "新确认后的买入金额",
            plan_id,
            subject=subject,
        )
        self._status(
            _C.OPTION_FEASIBILITY, FEASIBLE_WITH_APPROVAL, "新确认后买入", plan_id, subject
        )

    def _candidates(self, summary: CandidateSummary) -> None:
        ref = summary.calc_result_id
        self._percent(_C.SECTOR_SHARE, summary.start_upper_pct, "当前占比（上限）", ref)
        self._percent(_C.TARGET_MAX, summary.target_max_pct, "目标上限", ref)
        self._count(_C.OPTION_FEASIBILITY, summary.passing_count, "满足条件的方案数", ref)
        briefs = [("推荐方案", summary.selected)] if summary.selected else []
        briefs += [(f"备选方案{n}", b) for n, b in enumerate(summary.alternatives, start=1)]
        for name, brief in briefs:
            self._brief(ref, name, brief)

    def _brief(self, ref: str, name: str, brief: CandidateBrief) -> None:
        subject = f"{ref}:{name}"
        for concept, value, suffix in (
            (_C.REDEMPTION_GROSS, brief.gross_fen, "赎回金额"),
            (_C.FEE, brief.fee_fen, "手续费"),
            (_C.PURCHASE_AMOUNT, brief.purchase_fen, "买入金额"),
        ):
            if value is not None:
                self._money(concept, value, f"{name}{suffix}", ref, subject=subject)
        self._percent(
            _C.SECTOR_SHARE, brief.upper_pct_at_checkpoint, f"{name}检查点占比（上限）", ref
        )

    def _exposure(self, view: ExposureView) -> None:
        result, ref = view.exposure, view.calc_result_id
        concept = _C.SECTOR_SHARE if result.group is ExposureGroup.SECTOR else _C.ISSUER_SHARE
        self._percent(concept, result.lower_pct, f"{result.key} 占比（下限）", ref, result.key)
        self._percent(concept, result.upper_pct, f"{result.key} 占比（上限）", ref, result.key)
        self._money(_C.EXPOSURE_VALUE, result.total_fen, "基金市值合计", ref)
        if result.target_max_pct is not None:
            self._percent(_C.TARGET_MAX, result.target_max_pct, "目标上限", ref)

    def _fee_quote(self, view: FeeEstimateView) -> None:
        quote, ref = view.quote, view.product_id
        for concept, value, suffix in (
            (_C.REDEMPTION_GROSS, quote.gross_fen, "赎回金额"),
            (_C.FEE, quote.fee_fen, "赎回费"),
            (_C.REDEMPTION_NET, quote.net_fen, "到账金额"),
        ):
            if value is not None:
                self._money(concept, value, f"{view.product_id}{suffix}", ref, subject=ref)

    # Names

    def _bill_name(self, commitment_id: str) -> str:
        bill = commitments.find_commitment(self.reader, commitment_id)
        return bill_label(bill.kind) if bill else "账单"

    def _action_label(self, action: actions.ActionRow) -> str:
        if action.kind in MANDATE_KIND_TEXT:
            ref = action.params.get("mandate_ref")
            merchant = mandates.merchant_display(self.reader, str(ref)) if ref else ""
            return f"{merchant}{MANDATE_KIND_TEXT[action.kind]}"
        if action.kind is ActionKind.PAY_BILL:
            return f"{self._bill_name(str(action.params.get('commitment_id')))}账单付款"
        payee_id = action.params.get("payee_id")
        payee = accounts.find_payee(self.reader, str(payee_id)) if payee_id else None
        if payee is not None:
            return f"转给{payee.display_name}"
        return KIND_TEXT.get(action.kind, "该操作")

    # Fact constructors

    def _money(
        self,
        concept: FactConcept,
        value: int,
        label: str,
        ref: str,
        *,
        subject: str | None = None,
        as_of: datetime | None = None,
    ) -> None:
        self._add(MoneyFact, concept, value, label, ref, subject=subject, as_of=as_of)

    def _percent(
        self,
        concept: FactConcept,
        value: Decimal,
        label: str,
        ref: str,
        subject: str | None = None,
        *,
        as_of: datetime | None = None,
    ) -> None:
        self._add(PercentFact, concept, value, label, ref, subject=subject, as_of=as_of)

    def _count(
        self,
        concept: FactConcept,
        value: int,
        label: str,
        ref: str,
        *,
        as_of: datetime | None = None,
    ) -> None:
        self._add(CountFact, concept, value, label, ref, as_of=as_of)

    def _status(
        self,
        concept: FactConcept,
        value: str,
        label: str,
        ref: str,
        subject: str | None,
        *,
        as_of: datetime | None = None,
    ) -> None:
        self._add(StatusFact, concept, str(value), label, ref, subject=subject, as_of=as_of)

    def _add(
        self,
        model: type[BaseModel],
        concept: FactConcept,
        value: Any,
        label: str,
        ref: str,
        *,
        subject: str | None = None,
        as_of: datetime | None = None,
    ) -> None:
        key = (model, concept, subject, label, str(value))
        if key in self._seen:
            return
        self._seen.add(key)
        fact = model(
            fact_id=f"f{len(self._facts) + 1}",
            concept=concept,
            subject=subject,
            source_ref=ref,
            as_of=as_of or self.as_of,
            label=label,
            value=value,
        )
        self._facts.append(fact)  # type: ignore[arg-type]


def collect_facts(reader: Reader, results: Iterable[BaseModel], as_of: datetime) -> FactBlock:
    """The typed facts of a turn, in the order the results came."""
    collector = FactCollector(reader, as_of)
    for result in results:
        collector.add(result)
    return collector.block()


def render_fact(fact: Fact) -> str:
    """One fact as a customer-facing line, from fixed templates."""
    match fact:
        case MoneyFact():
            text = format_yuan(fact.value)
        case PercentFact():
            text = f"{fact.value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"
        case CountFact():
            unit = "个" if fact.concept is _C.OPTION_FEASIBILITY else "笔"
            text = f"{fact.value}{unit}"
        case DateFact():
            text = f"{fact.value.year}年{fact.value.month}月{fact.value.day}日"
        case _:
            table = CONCEPT_STATUS_TEXT.get(fact.concept, STATUS_TEXT)
            text = table.get(str(fact.value), "状态待核实")
    return f"{fact.label}：{text}"


def format_yuan(fen: int) -> str:
    sign = "-" if fen < 0 else ""
    yuan, cents = divmod(abs(fen), 100)
    return f"{sign}¥{yuan:,}.{cents:02d}"


def fallback_explanation(block: FactBlock) -> str:
    """The template that replaces an explanation; it makes no claim of its own."""
    statuses = {
        f.value for f in block.facts if isinstance(f, StatusFact) and f.concept is _C.ACTION_STATUS
    }
    if ActionStatus.AWAITING_APPROVAL.value in statuses:
        return TEMPLATE_AWAITING
    if ActionStatus.DRAFT.value in statuses:
        return TEMPLATE_DRAFT
    return TEMPLATE_FACTS if block.facts else TEMPLATE_EMPTY


@dataclass(frozen=True)
class StatedValue:
    """A money (fen) or percent value as written, with the comparator written next to it."""

    kind: FactKind
    value: int | Decimal
    comparator: str | None


def customer_values(texts: Iterable[str]) -> frozenset[StatedValue]:
    """Values the customer wrote in the given messages, outside each message's remark."""
    values: set[StatedValue] = set()
    for text in texts:
        remark = Utterance.of(text).remark_span
        for amount in amounts_outside(text, remark):
            values.add(StatedValue(FactKind.MONEY, amount.fen, amount.comparator))
        for start, end, value in _percents(text):
            if remark is None or not (start < remark[1] and remark[0] < end):
                values.add(
                    StatedValue(FactKind.PERCENT, value, _percent_comparator(text, start, end))
                )
    return frozenset(values)


def _percents(text: str) -> list[tuple[int, int, Decimal]]:
    found = []
    for match in _PERCENT_RE.finditer(text):
        if match.group(1) is not None:
            value: Decimal | None = Decimal(match.group(1))
        elif re.fullmatch(r"\d+(?:\.\d+)?", match.group(2)):
            value = Decimal(match.group(2))
        else:
            number = parse_chinese_numeral(match.group(2))
            value = None if number is None else Decimal(number)
        if value is not None:
            found.append((match.start(), match.end(), value))
    return found


def _percent_comparator(text: str, start: int, end: int) -> str | None:
    """The limit written before a percent ("不超过40%") or right after it ("40%以下")."""
    before = _PERCENT_COMPARATOR_BEFORE_RE.search(text[max(0, start - 8) : start])
    if before is not None:
        return _PERCENT_COMPARATOR_WORDS[before.group(1)]
    after = _PERCENT_COMPARATOR_AFTER_RE.match(text, end)
    return _PERCENT_COMPARATOR_AFTER[after.group(1)] if after is not None else None


def _reply_values(text: str) -> dict[tuple[int, int], StatedValue]:
    """The reply's money and percent values by span, as the claim checker found them."""
    values = {
        (m.start, m.end): StatedValue(FactKind.MONEY, m.fen, m.comparator)
        for m in find_amounts(text)
    }
    for start, end, value in _percents(text):
        values[(start, end)] = StatedValue(
            FactKind.PERCENT, value, _percent_comparator(text, start, end)
        )
    return values


def _sentences(text: str) -> list[tuple[int, int]]:
    """Spans of the sentences of the text, each with its closing mark."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        spans.append((start, match.end()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _sentence_of(text: str, position: int) -> str:
    return next((text[a:b] for a, b in _sentences(text) if a <= position < b), "")


def _states_account(sentence: str) -> bool:
    """Whether the sentence speaks about the account, an outcome or a status."""
    if any(word in sentence for word in (*ACCOUNT_STATE_WORDS, *OUTCOME_WORDS)):
        return True
    return any(
        v.claim_kind is FactKind.STATUS for v in check_claims(sentence, _NO_FACTS).violations
    )


def restates_customer(
    violation: ClaimViolation,
    text: str,
    stated: frozenset[StatedValue],
    reply_values: dict[tuple[int, int], StatedValue],
) -> bool:
    """Whether a flagged claim only repeats a value the customer wrote (SPEC §17, K7-1).

    The value must come with the comparator the customer wrote next to it (none for none), so
    a limit is never restated as a plain figure. A limit is required against a fact of another
    value, and in a sentence about a fee, where a plain figure would state the fee itself.
    """
    if violation.reason is _R.UNSUPPORTED:
        needs_comparator = False
    elif violation.reason in COMPARATOR_REASONS:
        needs_comparator = True
    else:
        return False
    claim = reply_values.get((violation.start, violation.end))
    if claim is None or claim.kind is not violation.claim_kind:
        return False
    sentence = _sentence_of(text, violation.start)
    if _states_account(sentence):
        return False
    if needs_comparator or any(word in sentence for word in FEE_WORDS):
        return claim.comparator is not None and claim in stated
    return claim in stated


def remaining_violations(
    text: str, block: FactBlock, customer_texts: Sequence[str] = ()
) -> tuple[ClaimViolation, ...]:
    """The claim violations of the explanation that are not restated customer values."""
    check = check_claims(text, block)
    if check.passed:
        return ()
    stated = customer_values(customer_texts)
    if not stated:
        return check.violations
    values = _reply_values(text)
    return tuple(v for v in check.violations if not restates_customer(v, text, stated, values))


def verify_reply(
    db: Database,
    customer_id: str,
    explanation: str | None,
    results: Sequence[BaseModel],
    as_of: datetime,
    *,
    customer_texts: Sequence[str] = (),
) -> VerifiedReply:
    """Build the fact block and check the explanation; an unsupported claim replaces it.

    `customer_texts` are the masked messages the customer wrote that the model received for the
    turn; a money or percent claim that only restates one of their values is not a violation.
    In a turn without a draft, a question whose only violations are unsupported claims keeps
    its other sentences when they pass on their own; that is audited as a replacement.
    """
    with db.read() as conn:
        block = collect_facts(conn, results, as_of)
    lines = tuple(render_fact(fact) for fact in block.facts)
    text = (explanation or "").strip()
    if not text:
        return VerifiedReply(block, lines, None, replaced=False)
    violations = remaining_violations(text, block, customer_texts)
    if not violations:
        return VerifiedReply(block, lines, text, replaced=False)
    trimmed = trimmed_question(text, block, violations) if _no_draft(results) else None
    if trimmed is not None:
        _audit_replacement(db, customer_id, violations, TRIMMED_DECISION)
        return VerifiedReply(block, lines, trimmed, replaced=True, violations=violations)
    _audit_replacement(db, customer_id, violations)
    return VerifiedReply(
        block, lines, fallback_explanation(block), replaced=True, violations=violations
    )


def trimmed_question(
    text: str, block: FactBlock, violations: Sequence[ClaimViolation]
) -> str | None:
    """A clarifying question without the sentences that hold unsupported claims, if it stands.

    Only unsupported claims may be cut out; the rest must pass the claim check as it is and
    still ask the customer something. Otherwise None, and the explanation is replaced.
    """
    if any(v.reason is not _R.UNSUPPORTED for v in violations):
        return None
    kept = [
        text[a:b]
        for a, b in _sentences(text)
        if not any(v.start < b and a < v.end for v in violations)
    ]
    rest = "".join(kept).strip()
    if "？" not in rest and "?" not in rest:
        return None
    return rest if check_claims(rest, block).passed else None


def _no_draft(results: Sequence[BaseModel]) -> bool:
    drafts = (DraftResult, PlanDraftResult, ApprovalRequestResult)
    return not any(isinstance(result, drafts) for result in results)


def _audit_replacement(
    db: Database, customer_id: str, violations: Sequence[ClaimViolation], *extra: str
) -> None:
    decision = sorted({v.reason.value for v in violations} | set(extra))
    db.run_in_uow(
        lambda uow: audit.record(
            uow,
            AuditActor.SYSTEM,
            REPLACED_EVENT,
            customer_id=customer_id,
            decision=",".join(decision)[:64],
            reason=str(len(violations)),
        ),
        "audit replaced explanation",
    )
