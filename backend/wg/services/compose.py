"""Plan composition (SPEC §15.1, §19.2): the customer's drafts joined into one plan and its card.

The drafts are the guided UI's own: plan-less drafts (a reserve, bill payments, transfers)
and the nodes of a rebalance plan drafted from a fund search but not yet approved. The
request's `depends_on` is the whole dependency map of the new plan. A bill payment is due
at 09:00 on its bill's due date. The validator rejects the plan before any card exists;
otherwise one unit creates the plan (kind `mixed`), moves the drafts into it and opens the
plan card. A rebalance plan whose nodes moved is closed (`partial`), so its own card can no
longer be confirmed; its bounds, limits, target and calc results carry over to the new card.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from wg.contracts.api import CardKind, CardLine, MoneyChange, PercentChange, PlanDraftResult
from wg.contracts.enums import ActionKind, ActionStatus, AuditActor, PlanKind, PlanStatus, Tier
from wg.contracts.errors import ErrorClass, ErrorCode, WgError
from wg.contracts.plan import ComposePlanRequest, PlanValidationResult
from wg.core import k4_approval as k4
from wg.core.k1_session import owned
from wg.core.k3_policy import check_not_locked
from wg.core.k4_approval import CardContent
from wg.core.k5_coordinator import plan_node_terms
from wg.core.k5_validator import Bill, MoneyFacts, Proceeds, ProposedNode, validate_plan
from wg.k6 import (
    accounts,
    actions,
    approvals,
    audit,
    commitments,
    funds,
    operations,
    plans,
    reserves,
    snapshots,
)
from wg.k6.actions import ActionRow
from wg.k6.approvals import CardRow
from wg.k6.cash_inputs import cash_inputs_from_db
from wg.k6.db import Database, Reader, UnitOfWork
from wg.k6.plans import PlanRow
from wg.k6.simtime import SHANGHAI, now, to_db
from wg.numeric import compute_snapshot, redemption_fee
from wg.packs.base import fen_text
from wg.packs.funds import settlement_due
from wg.packs.labels import bill_label
from wg.services.funds import node_result
from wg.services.refusals import record_refusal
from wg.services.views import payee_display

BILL_DUE_TIME = time(9, 0)
FUND_KINDS = frozenset({ActionKind.REDEEM, ActionKind.PURCHASE, ActionKind.CHECK_OUTCOME})
# Lines of a rebalance card that stay on the composed card, in this order after the nodes.
CARRIED_LINES = ("fee", "turnover", "target", "bounds")


def _owner(action: ActionRow) -> str:
    return action.customer_id


class PlanComposer:
    def __init__(self, db: Database) -> None:
        self.db = db

    def compose(
        self,
        customer_id: str,
        request: ComposePlanRequest,
        actor: AuditActor = AuditActor.CUSTOMER,
    ) -> PlanDraftResult:
        """The composed plan and its card, or the validator's refusal with every code."""
        with self._audited(customer_id, request.draft_ids[0], actor):
            plan_id, nodes = self.db.run_in_uow(
                lambda uow: self._compose(uow, customer_id, request, actor), "compose plan"
            )
        return PlanDraftResult(
            plan_id=plan_id,
            kind=PlanKind.MIXED,
            validation=PlanValidationResult(ok=True),
            actions=tuple(node_result(node) for node in nodes),
        )

    def _compose(
        self,
        uow: UnitOfWork,
        customer_id: str,
        request: ComposePlanRequest,
        actor: AuditActor,
    ) -> tuple[str, list[ActionRow]]:
        at = now(uow)
        check_not_locked(uow, customer_id, None)
        drafts = [_draft(uow, customer_id, lid) for lid in request.draft_ids]
        sources = _source_plans(uow, drafts)
        constants = funds.plan_constants(uow, customer_id)
        today = at.astimezone(SHANGHAI).date()
        expires_at = datetime.combine(
            today + timedelta(days=constants.expiry_day + 1), time(0, 0), tzinfo=SHANGHAI
        )
        proposed = [
            ProposedNode(
                logical_action_id=draft.logical_action_id,
                kind=draft.kind,
                params=draft.params,
                depends_on=tuple(request.depends_on.get(draft.logical_action_id, ())),
                not_before=_not_before(uow, draft, at),
            )
            for draft in drafts
        ]
        facts = _money_facts(uow, customer_id, proposed, at, expires_at)
        verdict = validate_plan(proposed, facts)
        if not verdict.ok:
            raise WgError(verdict.codes[0], {"codes": [code.value for code in verdict.codes]})
        snapshot = snapshots.ensure_snapshot(uow, customer_id)
        snapshots.require_complete(snapshot)

        plan = plans.insert_plan(uow, customer_id, PlanKind.MIXED)
        nodes = [
            actions.assign_to_plan(
                uow, draft, plan.plan_id, depends_on=node.depends_on, not_before=node.not_before
            )
            for draft, node in zip(drafts, proposed, strict=True)
        ]
        source_card = None
        for source in sources:
            plans.set_status(uow, source, PlanStatus.PARTIAL)
            audit.record(
                uow,
                actor,
                "plan_merged",
                customer_id=customer_id,
                ref=source.plan_id,
                reason=plan.plan_id,
            )
            cards = approvals.cards_of_plan(uow, source.plan_id)
            if cards and cards[-1].kind is CardKind.PLAN:
                source_card = cards[-1]
        source_terms: Mapping[str, Any] = source_card.terms if source_card else {}
        terms = {
            "scope_kind": "plan",
            "customer_id": customer_id,
            "plan_id": plan.plan_id,
            "kind": PlanKind.MIXED.value,
            "nodes": plan_node_terms(uow, plan.plan_id),
            "target": source_terms.get("target"),
            "limits": source_terms.get("limits"),
            "bounds": source_terms.get("bounds"),
            "calc_result_id": source_terms.get("calc_result_id"),
            "stress_result_id": source_terms.get("stress_result_id"),
            "cash_snapshot_id": snapshot.snapshot_id,
            "expires_at": to_db(expires_at),
        }
        headroom = snapshot.values.headroom_fen
        content = _card_content(uow, nodes, facts, headroom, source_card, expires_at)
        card = k4.open_plan_card(
            uow,
            customer_id=customer_id,
            plan_id=plan.plan_id,
            terms=terms,
            content=content,
            expires_at=expires_at,
            cash_snapshot_id=snapshot.snapshot_id,
            calc_result_id=source_terms.get("calc_result_id"),
        )
        audit.record(
            uow,
            actor,
            "plan_composed",
            customer_id=customer_id,
            ref=plan.plan_id,
            decision=Tier.YELLOW.value,
            reason=card.card_id,
        )
        return plan.plan_id, nodes

    @contextmanager
    def _audited(self, customer_id: str, ref: str, actor: AuditActor) -> Iterator[None]:
        try:
            yield
        except WgError as error:
            if error.error_class is ErrorClass.EXPECTED:
                event = "plan_compose_refused"
                record_refusal(self.db, customer_id, event, ref, error, actor=actor)
            raise


def _draft(reader: Reader, customer_id: str, logical_action_id: str) -> ActionRow:
    """The customer's own DRAFT; another customer's or an unknown one is NOT_FOUND."""
    draft = owned(actions.find_current(reader, logical_action_id), customer_id, _owner)
    if draft.status is not ActionStatus.DRAFT:
        raise WgError(
            ErrorCode.UNSUPPORTED_REQUEST,
            {"reason": "not_a_draft", "logical_action_id": logical_action_id},
        )
    return draft


def _source_plans(reader: Reader, drafts: Sequence[ActionRow]) -> list[PlanRow]:
    """Unapproved plans the drafts come from; each must be taken whole."""
    chosen = {draft.logical_action_id for draft in drafts}
    sources: dict[str, PlanRow] = {}
    for draft in drafts:
        if draft.plan_id is None or draft.plan_id in sources:
            continue
        plan = plans.get_plan(reader, draft.plan_id)
        movable = (
            plan.kind is not PlanKind.SINGLE
            and plan.status is PlanStatus.DRAFT
            and plan.root_approval_id is None
        )
        whole = {node.logical_action_id for node in plans.nodes(reader, plan.plan_id)} <= chosen
        if not movable or not whole:
            raise WgError(
                ErrorCode.UNSUPPORTED_REQUEST,
                {"reason": "in_another_plan", "logical_action_id": draft.logical_action_id},
            )
        sources[plan.plan_id] = plan
    return list(sources.values())


def _not_before(reader: Reader, draft: ActionRow, at: datetime) -> datetime | None:
    """A bill is paid from 09:00 of its due date; other drafts keep their own time."""
    if draft.kind is not ActionKind.PAY_BILL:
        return draft.not_before
    bill = commitments.find_commitment(reader, str(draft.params["commitment_id"]))
    if bill is None:
        return None
    due = datetime.combine(bill.due_date, BILL_DUE_TIME, tzinfo=SHANGHAI)
    return due if due > at else None


def _money_facts(
    reader: Reader,
    customer_id: str,
    nodes: Sequence[ProposedNode],
    at: datetime,
    expires_at: datetime,
) -> MoneyFacts:
    available: dict[str, int] = {}
    covered: set[str] = set()
    for row in accounts.customer_accounts(reader, customer_id):
        available[row.account_id] = row.balance_fen - operations.reserved_fen(
            reader, row.account_id
        )
        if row.covered:
            covered.add(row.account_id)
    bills: dict[str, Bill] = {}
    proceeds: dict[str, Proceeds] = {}
    for node in nodes:
        if node.kind is ActionKind.PAY_BILL:
            bill = commitments.find_commitment(reader, str(node.params["commitment_id"]))
            if bill is not None and bill.customer_id == customer_id:
                bills[bill.commitment_id] = Bill(bill.status, bill.payee_id)
        elif node.kind is ActionKind.REDEEM:
            proceeds[node.logical_action_id] = _proceeds(reader, node.params, at)
    book = accounts.customer_payees(reader, customer_id)
    return MoneyFacts(
        now=at,
        expires_at=expires_at,
        available_fen=available,
        covered_accounts=frozenset(covered),
        headroom_fen=compute_snapshot(cash_inputs_from_db(reader, customer_id, at)).headroom_fen,
        reserve_fen=reserves.reserve_target(reader, customer_id),
        bills=bills,
        book=frozenset(p.payee_id for p in book),
        proceeds=proceeds,
        own_accounts={
            p.payee_id: own.account_id
            for p in book
            if (own := accounts.own_account(reader, p.payee_id)) is not None
        },
    )


def _proceeds(reader: Reader, params: Mapping[str, Any], at: datetime) -> Proceeds:
    """The redemption's net amount at today's NAV and its settlement time."""
    product = funds.get_product(reader, str(params["product_id"]))
    quote = redemption_fee(
        funds.lots_of(reader, str(params["account_id"]), product.product_id),
        Decimal(str(params["shares"])),
        product.nav,
        product.fee_schedule,
        at.astimezone(SHANGHAI).date(),
    )
    return Proceeds(settlement_due(at, product.redeem_settle_days), quote.net_fen)


def _when(moment: datetime) -> str:
    return moment.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")


def _card_content(
    reader: Reader,
    nodes: Sequence[ActionRow],
    facts: MoneyFacts,
    headroom_fen: int,
    source_card: CardRow | None,
    expires_at: datetime,
) -> CardContent:
    """The composed plan card: each node in plan order, then the carried fund limits."""
    carried = _carried(source_card)
    lines: list[CardLine] = []
    headroom_after = headroom_fen
    for node in nodes:
        if node.kind is ActionKind.DESIGNATE_RESERVE:
            target = int(node.params["target_fen"])
            headroom_after -= target - facts.reserve_fen
            text = f"{fen_text(facts.reserve_fen)} → {fen_text(target)}，确认计划时执行"
            lines.append(CardLine(key="reserve", label="应急储备", text=text))
        elif node.kind is ActionKind.PAY_BILL:
            lines.append(_bill_line(reader, node))
        elif node.kind is ActionKind.TRANSFER:
            payee = payee_display(reader, str(node.params["payee_id"])) or ""
            text = f"{fen_text(int(node.params['amount_fen']))} 给 {payee}"
            lines.append(CardLine(key=f"transfer_{len(lines)}", label="转账", text=text))
        elif node.kind in FUND_KINDS:
            key = {
                ActionKind.REDEEM: "redeem",
                ActionKind.PURCHASE: "purchase",
                ActionKind.CHECK_OUTCOME: "checkpoint",
            }[node.kind]
            if key in carried:
                lines.append(carried[key])
    lines.extend(carried[key] for key in CARRIED_LINES if key in carried)
    lines.append(CardLine(key="expires", label="授权有效至", text=f"{_when(expires_at)} 之前"))
    lines.extend(line for key, line in carried.items() if key.startswith("stress_"))

    changes: list[MoneyChange | PercentChange] = [
        MoneyChange(key="headroom", label="可自由支配", before=headroom_fen, after=headroom_after)
    ]
    warnings: list[str] = []
    if source_card is not None:
        content = source_card.content
        changes += [PercentChange.model_validate(c) for c in content.get("changes", [])]
        warnings += [w for w in content.get("warnings", []) if "验证码" not in w]
    warnings += [
        "每笔资金操作执行前都需要分别输入短信验证码。",
        "计划只支付列出的账单；之后确认的新账单需要单独支付。",
    ]
    return CardContent(
        title="组合计划",
        lines=tuple(lines),
        changes=tuple(changes),
        warnings=tuple(warnings),
    )


def _carried(source_card: CardRow | None) -> dict[str, CardLine]:
    if source_card is None:
        return {}
    lines = (CardLine.model_validate(line) for line in source_card.content.get("lines", []))
    return {line.key: line for line in lines}


def _bill_line(reader: Reader, node: ActionRow) -> CardLine:
    bill = commitments.get_commitment(reader, str(node.params["commitment_id"]))
    payee = payee_display(reader, bill.payee_id) or ""
    when = f"{_when(node.not_before)} 起" if node.not_before else "确认后"
    text = f"{fen_text(int(node.params['amount_fen']))} 付给 {payee}，{when}输入验证码后执行"
    label = f"支付{bill_label(bill.kind)}"
    return CardLine(key=f"bill_{bill.commitment_id}", label=label, text=text)
