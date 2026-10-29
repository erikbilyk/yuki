"""K5 continuation: the plan's purchase after its customer's bills changed (SPEC §14.6, §15.4).

The decision is B's `decide_purchase`, always against the root approval's baseline: x is
the sum of bills confirmed or increased since the approval's cash snapshot, each change
counted once. Within bounds a new revision replaces the purchase and needs a new code for
its exact amount; outside them only the purchase pauses. A purchase that was submitted is
reconciled, never revised, so only DRAFT, AWAITING_STEPUP and READY purchases are decided.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from wg.contracts.api import RevisionReason
from wg.contracts.engines import PlanBounds, RevisionDecision, RevisionOutcome
from wg.contracts.enums import ActionKind, ActionStatus, AuditActor
from wg.contracts.errors import ErrorCode
from wg.contracts.events import DomainEventType, PlanRevisionCreated
from wg.core import k4_approval as k4
from wg.core.k4_approval import InvalidationReason, StepUp
from wg.k6 import actions, approvals, audit, commitments, events, plans, snapshots
from wg.k6.actions import ActionRow
from wg.k6.approvals import ApprovalRow
from wg.k6.cash_inputs import cash_inputs_from_db
from wg.k6.db import Reader, UnitOfWork
from wg.k6.plans import PlanRow
from wg.numeric import compute_snapshot, decide_purchase

DECIDED = frozenset({ActionStatus.DRAFT, ActionStatus.AWAITING_STEPUP, ActionStatus.READY})


@dataclass(frozen=True)
class _Basis:
    root: ApprovalRow
    baseline_version: int
    decided_version: int


def _snapshot_version(reader: Reader, snapshot_id: str | None) -> int:
    snapshot = snapshots.find_snapshot(reader, snapshot_id) if snapshot_id else None
    return snapshot.commitments_version if snapshot is not None else 0


def _basis(reader: Reader, plan: PlanRow) -> _Basis | None:
    """The root approval, its snapshot's commitments version and that of the last revision."""
    if plan.root_approval_id is None:
        return None
    root = approvals.find_approval(reader, plan.root_approval_id)
    if root is None:
        return None
    baseline = _snapshot_version(reader, root.cash_snapshot_id)
    history = plans.revisions(reader, plan.plan_id)
    decided = _snapshot_version(reader, history[-1].cash_snapshot_id) if history else baseline
    return _Basis(root, baseline, decided)


def decide(reader: Reader, plan: PlanRow, node: ActionRow, at: datetime) -> RevisionDecision:
    """B's decision for the purchase now, from the original approval's baseline."""
    basis = _basis(reader, plan)
    assert basis is not None
    bounds = PlanBounds.model_validate(dict(basis.root.bounds)) if basis.root.bounds else None
    added = commitments.additions_after(reader, plan.customer_id, basis.baseline_version)
    headroom = compute_snapshot(cash_inputs_from_db(reader, plan.customer_id, at)).headroom_fen
    return decide_purchase(added, bounds, int(node.params["amount_fen"]), headroom)


def stale_purchase(reader: Reader, plan_id: str, params: Mapping[str, Any], at: datetime) -> bool:
    """STALE_SNAPSHOT (SPEC §12.3): bills were added after the purchase's revision was
    decided, and deciding again now would change or pause it."""
    plan = plans.find_plan(reader, plan_id)
    basis = _basis(reader, plan) if plan is not None else None
    if plan is None or basis is None:
        return False
    if commitments.additions_after(reader, plan.customer_id, basis.decided_version) == 0:
        return False
    node = next(
        (
            n
            for n in plans.nodes(reader, plan_id)
            if n.kind is ActionKind.PURCHASE and n.params.get("product_id") == params["product_id"]
        ),
        None,
    )
    if node is None:
        return False
    return decide(reader, plan, node, at).outcome is not RevisionOutcome.UNCHANGED


def reconsider(
    uow: UnitOfWork, step_up: StepUp, plan: PlanRow, node: ActionRow, at: datetime
) -> ActionRow:
    """Decide a purchase again when its customer's bills changed since its last decision.

    Returns the purchase as it stands afterwards: unchanged, a new revision, or PAUSED.
    An unchanged decision writes nothing; the next check decides again.
    """
    if node.kind is not ActionKind.PURCHASE or node.status not in DECIDED:
        return node
    basis = _basis(uow, plan)
    if basis is None:
        return node
    if commitments.commitments_version(uow, plan.customer_id) == basis.decided_version:
        return node
    snapshot = snapshots.ensure_snapshot(uow, plan.customer_id)
    if snapshot.incomplete:
        return pause(uow, step_up, node, (ErrorCode.DATA_INCOMPLETE,), at)
    decision = decide(uow, plan, node, at)
    if decision.outcome is RevisionOutcome.PAUSE:
        return pause(uow, step_up, node, decision.reasons, at)
    if decision.outcome is RevisionOutcome.REVISE:
        return revise(uow, step_up, plan, node, decision, snapshot.snapshot_id, basis.root, at)
    return node


def pause(
    uow: UnitOfWork,
    step_up: StepUp,
    node: ActionRow,
    reasons: tuple[ErrorCode, ...],
    at: datetime,
) -> ActionRow:
    """Pause the purchase only; its open code or grant closes. Other nodes go on."""
    k4.invalidate_grants(
        uow, step_up, InvalidationReason.PAUSED, at, logical_action_id=node.logical_action_id
    )
    current = actions.get_revision(uow, node.logical_action_id, node.revision)
    paused = actions.set_status(
        uow, current, ActionStatus.PAUSED, pause_reasons=[r.value for r in reasons]
    )
    audit.record(
        uow,
        AuditActor.COORDINATOR,
        "node_paused",
        customer_id=node.customer_id,
        ref=node.logical_action_id,
        decision=reasons[0].value,
        reason=",".join(r.value for r in reasons),
    )
    return paused


def revise(
    uow: UnitOfWork,
    step_up: StepUp,
    plan: PlanRow,
    node: ActionRow,
    decision: RevisionDecision,
    snapshot_id: str,
    root: ApprovalRow,
    at: datetime,
) -> ActionRow:
    """One unit: the new revision at P(x), the old one SUPERSEDED, its code or grant closed,
    and the plan revision recorded. The new amount needs a new code (SPEC §15.4)."""
    # Deferred: the funds pack imports this module for its STALE_SNAPSHOT check.
    from wg.packs.registry import pack_for

    k4.invalidate_grants(
        uow, step_up, InvalidationReason.NEW_REVISION, at, logical_action_id=node.logical_action_id
    )
    old = actions.get_revision(uow, node.logical_action_id, node.revision)
    actions.set_status(uow, old, ActionStatus.SUPERSEDED, superseded_by=old.revision + 1)
    pack = pack_for(ActionKind.PURCHASE)
    revised = {**old.params, "amount_fen": decision.purchase_fen}
    params = pack.canonical(ActionKind.PURCHASE, revised)
    new = actions.insert_action(
        uow,
        logical_action_id=old.logical_action_id,
        customer_id=old.customer_id,
        kind=ActionKind.PURCHASE,
        params=params,
        request_hash=pack.request_hash(ActionKind.PURCHASE, old.customer_id, params),
        tier=pack.tier(uow, old.customer_id, ActionKind.PURCHASE, params, at),
        revision=old.revision + 1,
        plan_id=plan.plan_id,
        depends_on=old.depends_on,
        not_before=old.not_before,
        provenance=old.provenance,
    )
    if old.status is not ActionStatus.DRAFT:
        new = actions.set_status(uow, new, ActionStatus.AWAITING_STEPUP)
    current = plans.get_plan(uow, plan.plan_id)
    current = plans.set_active_revision(uow, current, current.active_revision + 1)
    trigger = events.latest_event_id(uow, DomainEventType.COMMITMENTS_CHANGED, plan.customer_id)
    plans.insert_revision(
        uow,
        plan_id=plan.plan_id,
        revision=current.active_revision,
        reason=RevisionReason.BILL_CHANGE.value,
        cash_snapshot_id=snapshot_id,
        calc_result_id=root.calc_result_id,
        approval_id=root.approval_id,
        within_bounds=True,
        trigger_event_id=trigger,
    )
    events.append_event(
        uow,
        DomainEventType.PLAN_REVISION_CREATED,
        PlanRevisionCreated(
            plan_id=plan.plan_id,
            logical_action_id=new.logical_action_id,
            revision=new.revision,
            previous_revision=old.revision,
            reason=RevisionReason.BILL_CHANGE.value,
            within_bounds=True,
            trigger_event_id=trigger,
            cash_snapshot_id=snapshot_id,
            approval_id=root.approval_id,
        ),
        customer_id=plan.customer_id,
        plan_id=plan.plan_id,
    )
    audit.record(
        uow,
        AuditActor.COORDINATOR,
        "node_revised",
        customer_id=plan.customer_id,
        ref=new.logical_action_id,
        decision=RevisionOutcome.REVISE.value,
        reason=f"{old.params['amount_fen']}->{decision.purchase_fen}",
    )
    return new
