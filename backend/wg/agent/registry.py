"""The tool registry: every call the model can make passes through here (SPEC §18.2).

Order of checks: tool exists, channel, capability, arguments, read scope, then the handler.
Draft handlers check every quoted slot with K2 (`wg.core.k2_context`) before they call a
service. Handlers call `wg.services` only. A refusal comes back to the model as a `ToolError`
and is written to the audit log by the agent actor.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, NoReturn

from pydantic import BaseModel, ValidationError

from wg.contracts.api import (
    BillDraftRequest,
    BillsQuery,
    CandidatesRequest,
    CandidatesView,
    DebitDecisionRequest,
    DebitRequestsQuery,
    ExposureQuery,
    MandateChangeRequest,
    SpendingQuery,
    SubscriptionSpendQuery,
    TransactionsQuery,
)
from wg.contracts.engines import Candidate
from wg.contracts.enums import (
    ActionKind,
    AuditActor,
    Channel,
    DebitDecision,
    PlanMode,
    PlanStatus,
    Provenance,
)
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.plan import ComposePlanRequest
from wg.contracts.tools import (
    TOOLS,
    TOOLS_BY_NAME,
    CandidateBrief,
    CandidateSummary,
    Capability,
    ComposePlanArgs,
    DataSource,
    DraftDebitDecisionArgs,
    DraftMandateChangeArgs,
    DraftTransferArgs,
    GetPlanStatusArgs,
    PayBillArgs,
    PlanStatusResult,
    ProposeBillArgs,
    ProposeReserveChangeArgs,
    SearchRebalanceCandidatesArgs,
    ToolArgs,
    ToolError,
    ToolSpec,
    tool_schemas,
)
from wg.core.k2_context import (
    Utterance,
    decision_quote_states,
    limit_is_strict,
    model_payload,
)
from wg.core.k3_policy import EXTRA_READ_EVENT, check_read_scope
from wg.k6 import audit
from wg.k6.db import Database
from wg.services.actions import ActionService, PayBillDraft, Slot, TransferDraft
from wg.services.cash import CashService
from wg.services.compose import PlanComposer
from wg.services.funds import FundsService
from wg.services.mandates import MandateService
from wg.services.plans import PlanService
from wg.services.reads import ReadService

TOOL_REFUSED_EVENT = "tool_refused"
OPEN_PLAN_STATUSES = frozenset(
    {PlanStatus.DRAFT, PlanStatus.ACTIVE, PlanStatus.PAUSED, PlanStatus.LOCKED}
)
MAX_ALTERNATIVES = 3
BOTH_CAPABILITIES = frozenset({Capability.READ, Capability.DRAFT})


@dataclass(frozen=True)
class ToolContext:
    """Who a tool call is for and what it may do (SPEC §6).

    `read_scope` further limits the sources a call may read, on top of the declared intent;
    None leaves the intent as the only limit. `utterance` is the customer's message of this turn.
    `mode` is the session's agent mode (SPEC §26).
    """

    customer_id: str
    channel: Channel
    capabilities: frozenset[Capability] = BOTH_CAPABILITIES
    read_scope: frozenset[DataSource] | None = None
    utterance: str = ""
    mode: PlanMode = PlanMode.COORDINATED


@dataclass(frozen=True)
class ToolOutcome:
    """What one call produced: a result, or the refusal the model is told about."""

    name: str
    arguments: ToolArgs | None
    result: BaseModel | None = None
    error: ToolError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def payload(self) -> str:
        """The JSON text handed back to the model, reduced by K2."""
        body = self.error if self.error is not None else self.result
        assert body is not None
        return model_payload(body)


@dataclass(frozen=True)
class Services:
    reads: ReadService
    cash: CashService
    funds: FundsService
    plans: PlanService
    actions: ActionService
    composer: PlanComposer
    mandates: MandateService

    @classmethod
    def from_state(cls, state: Any) -> Services:
        """The services of a running application (`app.state`)."""
        return cls(
            reads=state.reads,
            cash=state.cash,
            funds=state.funds,
            plans=state.plans,
            actions=state.actions,
            composer=state.composer,
            mandates=state.mandates,
        )


# A checked slot: its name, quote, and the parsed check to run once the quote is the customer's.
QuotedSlot = tuple[str, str | None, Callable[[Utterance], None] | None]


def candidate_summary(view: CandidatesView, target_max_pct: Decimal) -> CandidateSummary:
    """The model's view of a candidate search: counts and a few briefs, never the grid."""
    result = view.result
    passing = [candidate for candidate in result.candidates if candidate.passes]
    reasons = Counter(
        reason
        for candidate in result.candidates
        if not candidate.passes
        for reason in candidate.reasons
    )
    others = [candidate for candidate in passing if candidate != result.selected]
    return CandidateSummary(
        calc_result_id=view.calc_result_id,
        cash_snapshot_id=result.cash_snapshot_id,
        start_upper_pct=result.start.upper_pct,
        target_max_pct=target_max_pct,
        selected=_brief(result.selected) if result.selected else None,
        passing_count=len(passing),
        rejected_counts=dict(reasons),
        alternatives=tuple(_brief(candidate) for candidate in others[:MAX_ALTERNATIVES]),
    )


def _brief(candidate: Candidate) -> CandidateBrief:
    return CandidateBrief(
        kind=candidate.kind,
        sell_product=candidate.sell_product,
        buy_product=candidate.buy_product,
        gross_fen=candidate.gross_fen,
        fee_fen=candidate.fee_fen,
        purchase_fen=candidate.purchase_fen,
        upper_pct_at_checkpoint=candidate.checkpoint.upper_pct,
        passes=candidate.passes,
        reasons=candidate.reasons,
    )


class ToolRegistry:
    def __init__(self, db: Database, services: Services) -> None:
        self.db = db
        self.services = services
        self._handlers: Mapping[str, Callable[[ToolContext, Any], BaseModel]] = {
            "get_accounts": lambda c, a: services.reads.accounts(c.customer_id),
            "get_balance": lambda c, a: services.reads.balance(c.customer_id, a.account_id),
            "search_payees": lambda c, a: services.reads.payees(c.customer_id, a.query),
            "get_transfer_history": lambda c, a: services.reads.history(
                c.customer_id, a.days, a.payee_ref
            ),
            "get_operation_status": lambda c, a: services.reads.action_status(
                c.customer_id, a.logical_action_id, a.operation_id
            ),
            "draft_transfer": self._draft_transfer,
            "request_approval": lambda c, a: services.actions.request_approval(
                c.customer_id, a.logical_action_id, AuditActor.AGENT
            ),
            "cancel_action": lambda c, a: services.actions.cancel(
                c.customer_id, a.logical_action_id, AuditActor.AGENT
            ),
            "get_cash_snapshot": lambda c, a: services.cash.snapshot(c.customer_id),
            "get_bills": lambda c, a: services.cash.bills(
                c.customer_id, BillsQuery(status=a.status, horizon_days=a.horizon_days)
            ),
            "get_spending": lambda c, a: services.cash.spending(
                c.customer_id, SpendingQuery(days=a.days, group_by=a.group_by)
            ),
            "get_transactions": lambda c, a: services.cash.transactions(
                c.customer_id,
                TransactionsQuery(
                    account_id=a.account_id, days=a.days, category=a.category, limit=a.limit
                ),
            ),
            "get_unresolved": lambda c, a: services.cash.unresolved(c.customer_id),
            "propose_bill": self._propose_bill,
            "propose_reserve_change": self._propose_reserve_change,
            "propose_resolution": lambda c, a: services.actions.draft_resolution(
                c.customer_id, a.txn_id, a.category, AuditActor.AGENT
            ),
            "pay_bill": self._pay_bill,
            "mark_paid_external": lambda c, a: services.actions.draft_bill_action(
                c.customer_id, ActionKind.MARK_PAID_EXTERNAL, a.commitment_id, AuditActor.AGENT
            ),
            "cancel_bill": lambda c, a: services.actions.draft_bill_action(
                c.customer_id, ActionKind.CANCEL_BILL, a.commitment_id, AuditActor.AGENT
            ),
            "get_portfolio_exposure": lambda c, a: services.funds.exposure(
                c.customer_id,
                ExposureQuery(
                    group=a.group, key=a.key, target_max_pct=a.target_max_pct, basis=a.basis
                ),
            ),
            "get_positions": lambda c, a: services.funds.positions(c.customer_id),
            "search_rebalance_candidates": self._search_candidates,
            "estimate_redemption_fee": lambda c, a: services.funds.fee_estimate(
                c.customer_id, a.product_id, a.shares
            ),
            "run_stress": lambda c, a: services.funds.stress(c.customer_id, a.calc_result_id),
            "get_plan_status": self._plan_status,
            "draft_rebalance_plan": lambda c, a: services.funds.draft_rebalance_plan(
                c.customer_id, a.calc_result_id, a.with_bounds, AuditActor.AGENT, c.mode
            ),
            "compose_plan": self._compose_plan,
            "list_mandates": lambda c, a: services.mandates.mandates(c.customer_id),
            "get_debit_requests": lambda c, a: services.mandates.debit_requests(
                c.customer_id, DebitRequestsQuery(status=a.status, mandate_ref=a.mandate_ref)
            ),
            "get_subscription_spend": lambda c, a: services.mandates.subscription_spend(
                c.customer_id, SubscriptionSpendQuery(days=a.days)
            ),
            "draft_mandate_change": self._draft_mandate_change,
            "draft_debit_decision": self._draft_debit_decision,
        }
        missing = {tool.name for tool in TOOLS} - set(self._handlers)
        if missing:
            raise RuntimeError(f"tools without a handler: {', '.join(sorted(missing))}")

    def schemas(self, context: ToolContext) -> list[dict[str, Any]]:
        """Tool definitions for the context's channel and capabilities."""
        allowed = {tool.name for tool in TOOLS if tool.capability in context.capabilities}
        return [
            schema
            for schema in tool_schemas(context.channel)
            if schema["function"]["name"] in allowed
        ]

    def call(
        self, context: ToolContext, name: str, raw_arguments: Mapping[str, Any]
    ) -> ToolOutcome:
        """Run one tool call and return its result or the refusal; arguments are never trusted."""
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            return self._refused(
                context, name, ErrorCode.UNSUPPORTED_REQUEST, {"reason": "unknown_tool"}
            )
        if context.channel not in spec.channels:
            return self._refused(
                context, name, ErrorCode.UNSUPPORTED_REQUEST, {"reason": "channel"}
            )
        if spec.capability not in context.capabilities:
            return self._refused(context, name, ErrorCode.FORBIDDEN_ROUTE, {"reason": "capability"})
        try:
            arguments = spec.arguments.model_validate(dict(raw_arguments))
        except ValidationError as error:
            fields = sorted(
                {".".join(str(part) for part in item["loc"]) for item in error.errors()}
            )
            return self._refused(
                context, name, ErrorCode.UNSUPPORTED_REQUEST, {"fields": fields}, reason="arguments"
            )
        try:
            self._check_scope(context, spec, arguments)
            result = self._handlers[name](context, arguments)
        except WgError as error:
            return ToolOutcome(
                name, arguments, error=ToolError(code=error.code, detail=error.detail)
            )
        return ToolOutcome(name, arguments, result=result)

    def _check_scope(self, context: ToolContext, spec: ToolSpec, arguments: ToolArgs) -> None:
        check_read_scope(
            self.db,
            customer_id=context.customer_id,
            tool=spec.name,
            intent=arguments.intent,
            sources=spec.sources,
        )
        if context.read_scope is None:
            return
        extra = spec.sources - context.read_scope
        if extra:
            names = ",".join(sorted(source.value for source in extra))
            self._audit(
                context, spec.name, EXTRA_READ_EVENT, ErrorCode.READ_SCOPE_DENIED, reason=names
            )
            raise WgError(
                ErrorCode.READ_SCOPE_DENIED,
                {"tool": spec.name, "sources": sorted(source.value for source in extra)},
            )

    def _refused(
        self,
        context: ToolContext,
        name: str,
        code: ErrorCode,
        detail: dict[str, Any],
        *,
        reason: str | None = None,
    ) -> ToolOutcome:
        self._audit(context, name, TOOL_REFUSED_EVENT, code, reason=reason or detail.get("reason"))
        return ToolOutcome(name, None, error=ToolError(code=code, detail=detail))

    def _audit(
        self, context: ToolContext, name: str, event: str, code: ErrorCode, *, reason: str | None
    ) -> None:
        self.db.run_in_uow(
            lambda uow: audit.record(
                uow,
                AuditActor.AGENT,
                event,
                customer_id=context.customer_id,
                ref=name[:64],
                decision=code.value,
                reason=reason[:64] if reason else None,
            ),
            "audit tool refusal",
        )

    def _refuse(
        self, context: ToolContext, name: str, code: ErrorCode, detail: dict[str, Any]
    ) -> NoReturn:
        self._audit(
            context, name, TOOL_REFUSED_EVENT, code, reason=",".join(detail.get("slots", []))
        )
        raise WgError(code, detail)

    def _check_slots(
        self,
        context: ToolContext,
        name: str,
        utterance: Utterance,
        slots: Iterable[QuotedSlot],
        *,
        refuse_untrusted: bool = True,
    ) -> None:
        """K2: quotes must come from the message outside the remark and state the value."""
        slots = [slot for slot in slots if slot[1] is not None]
        untrusted = [
            slot
            for slot, quote, _ in slots
            if utterance.provenance(quote) is Provenance.UNTRUSTED_TEXT
        ]
        if untrusted:
            if refuse_untrusted:
                self._refuse(context, name, ErrorCode.UNTRUSTED_SOURCE, {"slots": untrusted})
            return
        for slot, _, check in slots:
            if check is None:
                continue
            try:
                check(utterance)
            except WgError as error:
                self._refuse(context, name, error.code, {"slots": [slot]})

    # Draft handlers that read quotes

    def _draft_transfer(self, context: ToolContext, args: DraftTransferArgs) -> BaseModel:
        utterance = Utterance.of(context.utterance, args.remark)
        # An untrusted payee or amount is refused and audited by the draft service itself.
        self._check_slots(
            context,
            "draft_transfer",
            utterance,
            [
                (
                    "payee_ref",
                    args.payee_quote,
                    lambda u: self._check_payee(context, args.payee_ref, args.payee_quote),
                ),
                (
                    "amount_fen",
                    args.amount_quote,
                    lambda u: u.check_amount("amount_fen", args.amount_quote, args.amount_fen),
                ),
            ],
            refuse_untrusted=False,
        )
        draft = TransferDraft(
            payee=_slot(utterance, args.payee_ref, args.payee_quote),
            amount=_slot(utterance, args.amount_fen, args.amount_quote),
            account_id=args.account_id,
            remark=args.remark,
        )
        return self.services.actions.draft_transfer(context.customer_id, draft, AuditActor.AGENT)

    def _check_payee(self, context: ToolContext, payee_ref: str, quote: str) -> None:
        """The customer's words must name exactly one payee, and the model must have chosen it.

        "王" names both 王磊 and 王芳: the model has to ask, not pick one (T3).
        """
        named = [
            payee.payee_id
            for payee in self.services.reads.payees(context.customer_id).payees
            if _names(quote, payee.alias) or _names(quote, payee.display_name)
        ]
        if len(named) != 1:
            raise WgError(ErrorCode.AMBIGUOUS_PAYEE, {"matches": len(named)})
        if named[0] != payee_ref:
            raise WgError(ErrorCode.PAYEE_MISMATCH, {})

    def _pay_bill(self, context: ToolContext, args: PayBillArgs) -> BaseModel:
        amount = None
        if args.amount_fen is not None and args.amount_quote is not None:
            utterance = Utterance.of(context.utterance)
            fen, quote = args.amount_fen, args.amount_quote
            self._check_slots(
                context,
                "pay_bill",
                utterance,
                [("amount_fen", quote, lambda u: u.check_amount("amount_fen", quote, fen))],
                refuse_untrusted=False,
            )
            amount = _slot(utterance, fen, quote)
        draft = PayBillDraft(
            commitment=Slot(args.commitment_id, Provenance.SERVER_RECORD),
            amount=amount,
            account_id=args.account_id,
        )
        return self.services.actions.draft_pay_bill(context.customer_id, draft, AuditActor.AGENT)

    def _propose_bill(self, context: ToolContext, args: ProposeBillArgs) -> BaseModel:
        if args.commitment_id is None:
            absent = [
                name
                for name, value in (
                    ("payee_ref", args.payee_ref),
                    ("amount_fen", args.amount_fen),
                    ("due_date", args.due_date),
                )
                if value is None
            ]
            if absent:
                self._refuse(context, "propose_bill", ErrorCode.MISSING_SLOT, {"slots": absent})
        self._check_slots(
            context,
            "propose_bill",
            Utterance.of(context.utterance),
            [
                ("payee_ref", args.payee_quote, None),
                (
                    "amount_fen",
                    args.amount_quote,
                    lambda u: u.check_amount("amount_fen", str(args.amount_quote), args.amount_fen),
                ),
            ],
        )
        request = BillDraftRequest(
            commitment_id=args.commitment_id,
            purpose=args.purpose,
            payee_id=args.payee_ref,
            amount_fen=args.amount_fen,
            due_date=args.due_date,
        )
        return self.services.actions.draft_bill(context.customer_id, request, AuditActor.AGENT)

    def _propose_reserve_change(
        self, context: ToolContext, args: ProposeReserveChangeArgs
    ) -> BaseModel:
        self._check_slots(
            context,
            "propose_reserve_change",
            Utterance.of(context.utterance),
            [
                (
                    "target_fen",
                    args.amount_quote,
                    lambda u: u.check_amount("target_fen", args.amount_quote, args.target_fen),
                )
            ],
        )
        return self.services.actions.draft_reserve(
            context.customer_id, args.target_fen, AuditActor.AGENT
        )

    def _search_candidates(
        self, context: ToolContext, args: SearchRebalanceCandidatesArgs
    ) -> BaseModel:
        self._check_slots(
            context,
            "search_rebalance_candidates",
            Utterance.of(context.utterance),
            [
                (
                    "target_max_pct",
                    args.target_quote,
                    lambda u: u.check_percent(
                        "target_max_pct", args.target_quote, args.target_max_pct
                    ),
                ),
                (
                    "fee_cap_fen",
                    args.fee_cap_quote,
                    lambda u: u.check_amount("fee_cap_fen", args.fee_cap_quote, args.fee_cap_fen),
                ),
            ],
        )
        request = CandidatesRequest(
            sector=args.sector,
            target_max_pct=args.target_max_pct,
            target_inclusive=not limit_is_strict(args.target_quote),
            checkpoint_day=args.checkpoint_day,
            fee_cap_fen=args.fee_cap_fen,
            fee_cap_inclusive=not limit_is_strict(args.fee_cap_quote),
            turnover_cap_fen=args.turnover_cap_fen,
        )
        view = self.services.funds.candidates(context.customer_id, request)
        return candidate_summary(view, args.target_max_pct)

    def _plan_status(self, context: ToolContext, args: GetPlanStatusArgs) -> BaseModel:
        plans = self.services.plans
        if args.plan_id is not None:
            return PlanStatusResult(plans=(plans.plan(context.customer_id, args.plan_id),))
        open_ids = [
            summary.plan_id
            for summary in plans.plans(context.customer_id).plans
            if summary.status in OPEN_PLAN_STATUSES
        ]
        return PlanStatusResult(
            plans=tuple(plans.plan(context.customer_id, plan_id) for plan_id in open_ids)
        )

    def _compose_plan(self, context: ToolContext, args: ComposePlanArgs) -> BaseModel:
        if context.mode is PlanMode.FLAT:
            self._refuse(
                context, "compose_plan", ErrorCode.UNSUPPORTED_REQUEST, {"reason": "flat_mode"}
            )
        request = ComposePlanRequest(draft_ids=args.draft_ids, depends_on=args.depends_on)
        return self.services.composer.compose(context.customer_id, request, AuditActor.AGENT)

    def _draft_mandate_change(
        self, context: ToolContext, args: DraftMandateChangeArgs
    ) -> BaseModel:
        """K2: the quote names the agreement; a new cap is checked like an amount."""
        slots: list[QuotedSlot] = [
            (
                "mandate_ref",
                args.merchant_quote,
                lambda u: self._check_agreement(context, args.mandate_ref, args.merchant_quote),
            )
        ]
        if args.cap_fen is not None and args.cap_quote is not None:
            cap_fen, cap_quote = args.cap_fen, args.cap_quote
            slots.append(
                ("cap_fen", cap_quote, lambda u: u.check_amount("cap_fen", cap_quote, cap_fen))
            )
        self._check_slots(context, "draft_mandate_change", Utterance.of(context.utterance), slots)
        body = MandateChangeRequest(
            mandate_ref=args.mandate_ref, change=args.change, cap_fen=args.cap_fen
        )
        return self.services.mandates.draft_mandate_change(
            context.customer_id, body, AuditActor.AGENT
        )

    def _draft_debit_decision(
        self, context: ToolContext, args: DraftDebitDecisionArgs
    ) -> BaseModel:
        """K2: only the customer's own words pay or decline; the request's text never does."""
        slots: list[QuotedSlot] = [
            (
                "decision",
                args.decision_quote,
                lambda u: _check_decision(args.decision, args.decision_quote),
            )
        ]
        if args.merchant_quote is not None:
            merchant_quote = args.merchant_quote
            slots.append(
                (
                    "mandate_ref",
                    merchant_quote,
                    lambda u: self._check_agreement(
                        context, self._request_mandate(context, args.request_id), merchant_quote
                    ),
                )
            )
        self._check_slots(context, "draft_debit_decision", Utterance.of(context.utterance), slots)
        body = DebitDecisionRequest(request_id=args.request_id, decision=args.decision)
        return self.services.mandates.draft_debit_decision(
            context.customer_id, body, AuditActor.AGENT
        )

    def _request_mandate(self, context: ToolContext, request_id: str) -> str:
        return self.services.mandates.debit_request(context.customer_id, request_id).mandate_ref

    def _check_agreement(self, context: ToolContext, mandate_ref: str, quote: str) -> None:
        """The customer's words must name exactly one agreement, and it must be the model's."""
        names = self.services.mandates.agreement_names(context.customer_id)
        named = [ref for ref, known in names.items() if any(_names(quote, n) for n in known)]
        if len(named) != 1:
            raise WgError(ErrorCode.AMBIGUOUS_PAYEE, {"matches": len(named)})
        if named[0] != mandate_ref:
            raise WgError(ErrorCode.PAYEE_MISMATCH, {})


def _check_decision(decision: DebitDecision, quote: str) -> None:
    if not decision_quote_states(decision, quote):
        raise WgError(ErrorCode.MISSING_SLOT, {})


def _names(quote: str, name: str) -> bool:
    """A quote names a payee when it is part of the name or contains the whole name."""
    return quote in name or name in quote


def _slot[T](utterance: Utterance, value: T, quote: str) -> Slot[T]:
    return Slot(value, utterance.provenance(quote), quote)
