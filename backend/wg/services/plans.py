"""Plan views and the customer's stop (SPEC §8.2, §15, §19.2).

A plan view shows each logical action at its current revision with the phrase the plan
panel uses (S6 §8), the revision history, the checkpoint result and the four cash fields.
A lock is not a stored plan status; the view shows `locked` while a lock covers the plan.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

from wg.contracts.api import (
    NODE_PHRASE,
    CheckpointView,
    NextOperationView,
    PercentQuality,
    PlanNodeView,
    PlanRevisionView,
    PlanSummary,
    PlansView,
    PlanView,
    RevisionReason,
)
from wg.contracts.engines import PlanBounds
from wg.contracts.enums import (
    ActionKind,
    ActionStatus,
    CheckpointResult,
    PlanKind,
    PlanStatus,
)
from wg.contracts.errors import ErrorClass, ErrorCode, WgError
from wg.core import k5_coordinator as k5
from wg.core.k1_session import not_found, owned
from wg.core.k4_approval import StepUp
from wg.k6 import actions, approvals, challenges, commitments, locks, operations, plans
from wg.k6.actions import ActionRow
from wg.k6.db import Database, Reader
from wg.k6.plans import PlanRow
from wg.k6.simtime import now
from wg.services.cash import CashService
from wg.services.refusals import record_refusal
from wg.services.views import payee_display

_OPEN = frozenset(
    {
        ActionStatus.DRAFT,
        ActionStatus.AWAITING_APPROVAL,
        ActionStatus.AWAITING_STEPUP,
        ActionStatus.READY,
        ActionStatus.SUBMITTED,
        ActionStatus.UNKNOWN,
        ActionStatus.PAUSED,
    }
)


def _owner(plan: PlanRow) -> str:
    return plan.customer_id


class PlanService:
    def __init__(self, db: Database, step_up: StepUp, cash: CashService) -> None:
        self.db = db
        self.step_up = step_up
        self.cash = cash

    def plans(self, customer_id: str) -> PlansView:
        """The customer's plans; a standalone action's own plan (kind `single`) is not listed."""
        with self.db.read() as conn:
            rows = [
                plans.get_plan(conn, row["plan_id"])
                for row in conn.execute(
                    "SELECT plan_id FROM plans WHERE customer_id = ? AND kind <> ?"
                    " ORDER BY created_at, plan_id",
                    (customer_id, PlanKind.SINGLE.value),
                ).fetchall()
            ]
            return PlansView(plans=tuple(_summary(conn, plan) for plan in rows))

    def plan(self, customer_id: str, plan_id: str) -> PlanView:
        with self._audited(customer_id, "plan_read_refused", plan_id):
            with self.db.read() as conn:
                plan = _visible(conn, customer_id, plan_id)
                view = _plan_view(conn, plan)
        return view.model_copy(update={"cash": self.cash.cash_fields(customer_id)})

    def stop(self, customer_id: str, plan_id: str) -> PlanView:
        """The customer's "停止": unsubmitted nodes are cancelled and the plan is partial."""
        with self._audited(customer_id, "plan_stop_refused", plan_id):
            with self.db.read() as conn:
                _visible(conn, customer_id, plan_id)
            self.db.run_in_uow(
                lambda uow: k5.stop_plan(
                    uow, self.step_up, customer_id=customer_id, plan_id=plan_id, at=now(uow)
                ),
                "stop plan",
                plan_id=plan_id,
            )
        return self.plan(customer_id, plan_id)

    @contextmanager
    def _audited(self, customer_id: str, event: str, ref: str) -> Iterator[None]:
        try:
            yield
        except WgError as error:
            if error.error_class is ErrorClass.EXPECTED:
                record_refusal(self.db, customer_id, event, ref, error)
            raise


def _visible(reader: Reader, customer_id: str, plan_id: str) -> PlanRow:
    """The customer's plan; a standalone action's bookkeeping plan is not addressable."""
    plan = owned(plans.find_plan(reader, plan_id), customer_id, _owner)
    if plan.kind is PlanKind.SINGLE:
        raise not_found()
    return plan


def _status(reader: Reader, plan: PlanRow) -> PlanStatus:
    if not plan.final and locks.locks_in_scope(reader, plan.customer_id, plan.plan_id):
        return PlanStatus.LOCKED
    return plan.status


def _expires_at(reader: Reader, plan: PlanRow) -> datetime | None:
    if plan.root_approval_id is not None:
        root = approvals.find_approval(reader, plan.root_approval_id)
        if root is not None:
            return root.expires_at
    cards = approvals.cards_of_plan(reader, plan.plan_id)
    return cards[-1].expires_at if cards else None


def _summary(reader: Reader, plan: PlanRow) -> PlanSummary:
    return PlanSummary(
        plan_id=plan.plan_id,
        kind=plan.kind,
        status=_status(reader, plan),
        mode=plan.mode,
        created_at=plan.created_at,
        expires_at=_expires_at(reader, plan),
        next_operation=_next_operation(plans.nodes(reader, plan.plan_id)),
    )


def _next_operation(nodes: list[ActionRow]) -> NextOperationView | None:
    """The open node due soonest: by `not_before`, else in plan order."""
    open_nodes = [n for n in nodes if n.status in _OPEN and n.kind is not ActionKind.CHECK_OUTCOME]
    if not open_nodes:
        return None
    waiting = [n for n in open_nodes if n.status is not ActionStatus.DRAFT] or open_nodes
    node = min(waiting, key=lambda n: (n.not_before is not None, n.not_before or n.created_at))
    return NextOperationView(
        logical_action_id=node.logical_action_id,
        plan_id=node.plan_id,
        kind=node.kind,
        status=node.status,
        phrase=NODE_PHRASE[node.status],
        amount_fen=_amount(node),
        shares=_shares(node),
        not_before=node.not_before,
    )


def _plan_view(reader: Reader, plan: PlanRow) -> PlanView:
    nodes = plans.nodes(reader, plan.plan_id)
    cards = approvals.cards_of_plan(reader, plan.plan_id)
    bounds = None
    if plan.root_approval_id is not None:
        root = approvals.find_approval(reader, plan.root_approval_id)
        if root is not None and root.bounds is not None:
            bounds = PlanBounds.model_validate(dict(root.bounds))
    elif cards and cards[-1].terms.get("bounds"):
        bounds = PlanBounds.model_validate(cards[-1].terms["bounds"])
    summary = _summary(reader, plan)
    return PlanView(
        **summary.model_dump(),
        active_revision=plan.active_revision,
        root_approval_id=plan.root_approval_id,
        card_id=cards[-1].card_id if cards else None,
        bounds=bounds,
        nodes=tuple(_node_view(reader, node) for node in nodes),
        revisions=tuple(_revision_views(reader, plan, nodes)),
        checkpoint=_checkpoint_view(reader, plan, nodes),
    )


def _amount(node: ActionRow) -> int | None:
    amount = node.params.get("amount_fen")
    return amount if isinstance(amount, int) else None


def _shares(node: ActionRow) -> Decimal | None:
    shares = node.params.get("shares")
    return Decimal(str(shares)) if shares is not None else None


def _node_view(reader: Reader, node: ActionRow) -> PlanNodeView:
    operation = operations.find_by_key(
        reader, operations.idempotency_key(node.logical_action_id, node.revision)
    )
    receipt = operations.receipt_of(reader, operation.operation_id) if operation else None
    issued = challenges.challenges_of_action(reader, node.logical_action_id, node.revision)
    open_challenge = next((c for c in reversed(issued) if c.is_open), None)
    action_cards = approvals.cards_of_action(reader, node.logical_action_id, node.revision)
    payee_id = node.params.get("payee_id")
    commitment_id = node.params.get("commitment_id")
    due_date = None
    if commitment_id is not None:
        bill = commitments.find_commitment(reader, str(commitment_id))
        due_date = bill.due_date if bill is not None else None
    paused = node.status is ActionStatus.PAUSED
    return PlanNodeView(
        logical_action_id=node.logical_action_id,
        revision=node.revision,
        kind=node.kind,
        status=node.status,
        phrase=NODE_PHRASE[node.status],
        tier=node.tier,
        depends_on=node.depends_on,
        not_before=node.not_before,
        due_date=due_date,
        amount_fen=_amount(node),
        shares=operation.shares if operation and operation.shares else _shares(node),
        product_id=node.params.get("product_id"),
        payee_id=payee_id,
        payee_display=payee_display(reader, payee_id) if payee_id else None,
        commitment_id=commitment_id,
        pause_reasons=tuple(ErrorCode(r) for r in node.pause_reasons) if paused else (),
        challenge_expires_at=open_challenge.expires_at if open_challenge else None,
        card_id=action_cards[-1].card_id if action_cards else None,
        operation_id=operation.operation_id if operation else None,
        receipt_id=receipt.receipt_id if receipt else None,
        submitted_at=operation.submitted_at if operation else None,
        settled_at=operation.settled_at if operation else None,
    )


def _revision_views(
    reader: Reader, plan: PlanRow, nodes: list[ActionRow]
) -> Iterator[PlanRevisionView]:
    purchase = next((n for n in nodes if n.kind is ActionKind.PURCHASE), None)
    amounts: dict[int, Any] = {}
    if purchase is not None:
        for revision in range(1, purchase.revision + 1):
            row = actions.get_revision(reader, purchase.logical_action_id, revision)
            amounts[revision] = _amount(row)
    for row in plans.revisions(reader, plan.plan_id):
        if row.cash_snapshot_id is None:
            continue
        yield PlanRevisionView(
            revision=row.revision,
            reason=RevisionReason(row.reason),
            trigger_event_id=row.trigger_event_id,
            cash_snapshot_id=row.cash_snapshot_id,
            calc_result_id=row.calc_result_id,
            approval_id=row.approval_id,
            within_bounds=row.within_bounds,
            purchase_before_fen=amounts.get(row.revision - 1),
            purchase_after_fen=amounts.get(row.revision),
            created_at=row.created_at,
        )


def _checkpoint_view(
    reader: Reader, plan: PlanRow, nodes: list[ActionRow]
) -> CheckpointView | None:
    check = next((n for n in nodes if n.kind is ActionKind.CHECK_OUTCOME), None)
    if check is None:
        return None
    row = plans.find_checkpoint(reader, plan.plan_id, check.logical_action_id)
    if row is None:
        return None
    return CheckpointView(
        logical_action_id=row.logical_action_id,
        result=CheckpointResult(row.result),
        measured_pct=Decimal(row.measured_pct),
        lower_pct=Decimal(row.lower_pct),
        upper_pct=Decimal(row.upper_pct),
        target_max_pct=Decimal(str(check.params["target_max_pct"])),
        quality=PercentQuality.CONFIRMED,
        evaluated_at=row.evaluated_at,
    )
