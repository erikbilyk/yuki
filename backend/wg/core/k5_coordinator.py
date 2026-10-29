"""K5: the coordinator, the simulated clock walk and restart recovery (SPEC §12.8, §15).

The coordinator never runs in the background. A tick is triggered by an event: a clock
move, process start, or the domain events read through the coordinator's cursor. Each plan
is decided in its own unit of work; the READY nodes it submits go through the gateway, one
unit each. Every transition checks the stored status first, so a second tick, or two ticks
at once, change nothing more than one.

The coordinator uses only authority the customer gave: it submits a READY node whose
approval holds and, for a RED node, whose grant is still valid; otherwise the node waits
for a new code (AWAITING_STEPUP). It never creates an approval, a challenge or a grant.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from wg.contracts.enums import (
    ActionKind,
    ActionStatus,
    AuditActor,
    CheckpointResult,
    Fault,
    PlanKind,
    PlanStatus,
    Tier,
)
from wg.contracts.errors import ErrorCode, WgError
from wg.core import k4_approval as k4
from wg.core.k1_session import owned
from wg.core.k4_approval import InvalidationReason, StepUp
from wg.k6 import actions, approvals, audit, events, faults, locks, operations, plans
from wg.k6.actions import ActionRow
from wg.k6.clock import advance_clock
from wg.k6.db import Database, UnitOfWork
from wg.k6.gateway import CRASH_EXIT_STATUS, Gateway, ResponseDropped, reconcile_operation
from wg.k6.plans import PlanRow
from wg.k6.simtime import ClockError, ClockState, now, read_clock

log = logging.getLogger("wg.k5")

CURSOR = "coordinator"

UNSUBMITTED = frozenset(
    {
        ActionStatus.DRAFT,
        ActionStatus.AWAITING_APPROVAL,
        ActionStatus.AWAITING_STEPUP,
        ActionStatus.READY,
        ActionStatus.PAUSED,
    }
)
TERMINAL = frozenset(
    {
        ActionStatus.SETTLED,
        ActionStatus.REJECTED,
        ActionStatus.CANCELLED,
        ActionStatus.SUPERSEDED,
    }
)
_FAILED = frozenset({ActionStatus.REJECTED, ActionStatus.CANCELLED})


@dataclass
class _Decision:
    """What one plan's decision unit leaves for the gateway."""

    customer_id: str | None = None
    submit: list[str] = field(default_factory=list)


class Coordinator:
    def __init__(
        self,
        db: Database,
        gateway: Gateway,
        step_up: StepUp,
        *,
        exit_process: Callable[[int], object] = os._exit,
    ) -> None:
        self.db = db
        self.gateway = gateway
        self.step_up = step_up
        self._exit = exit_process

    # Ticks (SPEC §15.2)

    def tick(self, plan_ids: Iterable[str] | None = None) -> None:
        """Tick the given plans, or every plan that is not final or still has open operations.

        `tick_crash` ends the process after the first plan's decisions commit, before its
        submissions and before the other plans (T77).
        """
        with self.db.read() as conn:
            ids = plans.plans_to_tick(conn) if plan_ids is None else list(plan_ids)
            crash_armed = any(f.fault is Fault.TICK_CRASH for f in faults.armed_faults(conn))
        crash = crash_armed and faults.take(self.db, Fault.TICK_CRASH)
        if crash and not ids:
            self._crash()
        for plan_id in ids:
            self.tick_plan(plan_id, after_decisions=self._crash if crash else None)

    def tick_plan(
        self, plan_id: str, *, after_decisions: Callable[[], object] | None = None
    ) -> None:
        """Decide one plan in its own unit, then submit what it left READY, then its status."""
        try:
            decision = self.db.run_in_uow(
                lambda uow: self._decide(uow, plan_id), "coordinator tick", plan_id=plan_id
            )
        except WgError as error:
            # Recorded by run_in_uow when unexpected or an integrity violation (K8 may lock).
            log.warning("tick refused plan=%s code=%s", plan_id, error.code)
            return
        if after_decisions is not None:
            after_decisions()
        if not decision.submit:
            return
        assert decision.customer_id is not None
        for logical_action_id in decision.submit:
            self._submit(decision.customer_id, logical_action_id)
        self.db.run_in_uow(
            lambda uow: store_status(uow, plan_id), "coordinator plan status", plan_id=plan_id
        )

    def _decide(self, uow: UnitOfWork, plan_id: str) -> _Decision:
        at = now(uow)
        for operation in operations.open_operations(uow, plan_id=plan_id):
            reconcile_operation(uow, operation.operation_id)
        plan = plans.find_plan(uow, plan_id)
        if plan is None or plan.final:
            return _Decision()
        if locks.locks_in_scope(uow, plan.customer_id, plan_id):
            # Under a lock only observations run; CHECK_OUTCOME nodes come with S6 (A10).
            return _Decision()
        root = None
        if plan.root_approval_id is not None:
            root = approvals.find_approval(uow, plan.root_approval_id)
        if root is not None and at >= root.expires_at:
            self._expire(uow, plan, at)
            return _Decision()

        decision = _Decision(customer_id=plan.customer_id)
        for node in plans.nodes(uow, plan_id):
            if node.status is ActionStatus.READY and self._ready_to_submit(uow, node, at):
                decision.submit.append(node.logical_action_id)
        if root is not None and k4.approval_refusal(root, at) is None:
            decision.submit.extend(self._activate_drafts(uow, plan, at))
        store_status(uow, plan_id)
        return decision

    def _ready_to_submit(self, uow: UnitOfWork, node: ActionRow, at: datetime) -> bool:
        """True when the node's authority holds; a RED node without a valid grant waits for a code.

        A node whose approval is no longer valid is held: the gateway would refuse it, and the
        approval's expiry is handled with its plan.
        """
        if k4.approval_refusal(k4.approval_for(uow, node), at) is not None:
            return False
        if node.tier is not Tier.RED:
            return True
        grant = k4.find_grant(uow, node)
        if grant is not None and at < grant.expires_at:
            return True
        if grant is not None:
            k4.invalidate_grants(
                uow,
                self.step_up,
                InvalidationReason.EXPIRED,
                at,
                logical_action_id=node.logical_action_id,
            )
        current = actions.get_revision(uow, node.logical_action_id, node.revision)
        if current.status is ActionStatus.READY:
            actions.set_status(uow, current, ActionStatus.AWAITING_STEPUP)
        _audit_node(uow, node, "awaiting_code", ErrorCode.APPROVAL_REQUIRED.value)
        return False

    def _activate_drafts(self, uow: UnitOfWork, plan: PlanRow, at: datetime) -> list[str]:
        """Move DRAFT nodes whose dependencies settled and whose time came (SPEC §15.2 step 5).

        RED nodes wait for the customer's code; YELLOW ones are covered by the plan approval
        and are returned for submission. A node whose dependency failed is PAUSED (§15.5).
        """
        nodes = {node.logical_action_id: node for node in plans.nodes(uow, plan.plan_id)}
        ready = []
        for node in nodes.values():
            if node.status is not ActionStatus.DRAFT or node.kind is ActionKind.CHECK_OUTCOME:
                continue
            dependencies = [nodes[d] for d in node.depends_on if d in nodes]
            if any(d.status in _FAILED for d in dependencies):
                actions.set_status(
                    uow,
                    node,
                    ActionStatus.PAUSED,
                    pause_reasons=[ErrorCode.DEPENDENCY_FAILED.value],
                )
                _audit_node(uow, node, "node_paused", ErrorCode.DEPENDENCY_FAILED.value)
                continue
            if any(d.status is not ActionStatus.SETTLED for d in dependencies):
                continue
            if node.not_before is not None and node.not_before > at:
                continue
            if node.tier is Tier.RED:
                actions.set_status(uow, node, ActionStatus.AWAITING_STEPUP)
                _audit_node(uow, node, "awaiting_code", Tier.RED.value)
            else:
                actions.set_status(uow, node, ActionStatus.READY)
                _audit_node(uow, node, "node_ready", node.tier.value)
                ready.append(node.logical_action_id)
        return ready

    def _expire(self, uow: UnitOfWork, plan: PlanRow, at: datetime) -> None:
        """The plan approval expired: unsubmitted nodes are cancelled and the plan is partial."""
        cancel_unsubmitted(
            uow,
            self.step_up,
            plan.plan_id,
            at,
            reason=InvalidationReason.APPROVAL_EXPIRED,
            decision=ErrorCode.PLAN_APPROVAL_EXPIRED.value,
        )
        plans.set_status(uow, plans.get_plan(uow, plan.plan_id), PlanStatus.PARTIAL)
        _audit_plan(uow, plan, PlanStatus.PARTIAL)

    def _submit(self, customer_id: str, logical_action_id: str) -> None:
        try:
            self.gateway.submit(customer_id, logical_action_id)
        except ResponseDropped:
            # The submission committed; a tick has no response to drop.
            pass
        except WgError as error:
            log.warning("tick submission refused action=%s code=%s", logical_action_id, error.code)

    def _crash(self) -> None:
        log.warning("fault fired: tick_crash")
        self._exit(CRASH_EXIT_STATUS)

    # Events (SPEC §11)

    def process_events(self) -> int:
        """Tick the plans named by events after the cursor, then move the cursor past them.

        An event without a plan ticks its customer's plans; one without a customer (a clock
        move) ticks every plan. Returns the number of events processed.
        """
        with self.db.read() as conn:
            pending = events.unprocessed_events(conn, CURSOR)
            if not pending:
                return 0
            everything = any(e.plan_id is None and e.customer_id is None for e in pending)
            ids: set[str] = {e.plan_id for e in pending if e.plan_id is not None}
            for customer_id in {e.customer_id for e in pending if e.plan_id is None}:
                if customer_id is not None:
                    ids.update(plans.plans_to_tick(conn, customer_id))
        self.tick(None if everything else sorted(ids))
        last = pending[-1].event_id
        self.db.run_in_uow(lambda uow: events.mark_processed(uow, CURSOR, last), "event cursor")
        return len(pending)

    # Simulated clock (SPEC §15.3)

    def advance_clock(self, target: datetime) -> ClockState:
        """Walk the clock to `target` through every due instant in ascending order.

        At each instant: move the clock (codes whose five minutes are over close), apply the
        settlements due by then, then tick the plans. A target equal to now changes nothing;
        an earlier one is refused.
        """
        with self.db.read() as conn:
            start = read_clock(conn)
        if target < start.now:
            raise WgError(ErrorCode.UNSUPPORTED_REQUEST, {"reason": "clock_backwards"})
        if target == start.now:
            return start
        while True:
            with self.db.read() as conn:
                due = plans.next_due_instant(conn, now(conn), target)
            if due is None:
                break
            self._step(due)
        self._step(target)
        with self.db.read() as conn:
            return read_clock(conn)

    def _step(self, instant: datetime) -> None:
        self.db.run_in_uow(lambda uow: self._move_clock(uow, instant), "sim clock advance")
        self.gateway.settle_due()
        self.process_events()

    def _move_clock(self, uow: UnitOfWork, instant: datetime) -> None:
        # Another advance may already have moved past this instant; the clock never goes back.
        if instant <= now(uow):
            return
        try:
            state = advance_clock(uow, instant)
        except ClockError:
            return
        k4.expire_open_challenges(uow, self.step_up, state.now)

    # Restart (SPEC §12.8)

    def recover(self) -> None:
        """Bring the database to a consistent state at process start, without new authority.

        In order: replay unprocessed events; reconcile every SUBMITTED and UNKNOWN operation;
        apply settlements due by the current simulated time (the clock does not move); then
        tick every plan, which resubmits READY nodes whose grant is valid, moves the others to
        AWAITING_STEPUP and moves RED nodes that are due to AWAITING_STEPUP.

        A step that fails is logged and recovery goes on, so the process still starts; the
        failure itself is in `failure_records` and K8 may have locked its scope.
        """
        with self.db.read() as conn:
            try:
                read_clock(conn)
            except ClockError:
                return
            open_ids = [op.operation_id for op in operations.open_operations(conn)]
        steps: list[tuple[str, Callable[[], object]]] = [("events", self.process_events)]
        steps += [
            (f"reconcile {operation_id}", lambda i=operation_id: self.gateway.reconcile(i))
            for operation_id in open_ids
        ]
        steps += [("settlements", self.gateway.settle_due), ("tick", self.tick)]
        for name, step in steps:
            try:
                step()
            except WgError as error:
                log.warning("recovery step=%s code=%s", name, error.code)
            except Exception:
                log.exception("recovery step=%s failed", name)
        log.info("recovery done operations=%d", len(open_ids))


# Shared by the coordinator, cancel and stop


def store_status(uow: UnitOfWork, plan_id: str) -> PlanRow:
    """Derive the plan's status from its nodes and store it when it changed."""
    plan = plans.get_plan(uow, plan_id)
    status = derive_status(uow, plan)
    if status is plan.status:
        return plan
    updated = plans.set_status(uow, plan, status)
    _audit_plan(uow, plan, status)
    return updated


def derive_status(uow: UnitOfWork, plan: PlanRow) -> PlanStatus:
    """Plan status from its nodes (SPEC §8.2, §15.2 step 6).

    A lock is not stored as a status; readers see it from the locks table. `partial` after
    a stop or an expiry is set by those steps and is final.
    """
    if plan.final:
        return plan.status
    nodes = [n for n in plans.nodes(uow, plan.plan_id) if n.status is not ActionStatus.SUPERSEDED]
    if not nodes:
        return plan.status
    if plan.kind is PlanKind.SINGLE:
        status = nodes[-1].status
        if status is ActionStatus.SETTLED:
            return PlanStatus.COMPLETED
        if status in _FAILED:
            return PlanStatus.PARTIAL
        if status in (ActionStatus.DRAFT, ActionStatus.AWAITING_APPROVAL):
            return PlanStatus.DRAFT
        return PlanStatus.ACTIVE
    if plan.root_approval_id is None:
        return PlanStatus.DRAFT
    if all(_done(uow, plan, n) for n in nodes):
        return PlanStatus.COMPLETED
    open_nodes = [n for n in nodes if n.status not in TERMINAL]
    if not open_nodes:
        return PlanStatus.PARTIAL
    by_id = {n.logical_action_id: n for n in nodes}
    if all(_blocked_by_pause(n, by_id) for n in open_nodes):
        return PlanStatus.PAUSED
    return PlanStatus.ACTIVE


def _done(uow: UnitOfWork, plan: PlanRow, node: ActionRow) -> bool:
    if node.status is not ActionStatus.SETTLED:
        return False
    if node.kind is ActionKind.CHECK_OUTCOME:
        result = plans.checkpoint_result(uow, plan.plan_id, node.logical_action_id)
        return result == CheckpointResult.PASSED.value
    return True


def _blocked_by_pause(node: ActionRow, by_id: dict[str, ActionRow]) -> bool:
    """PAUSED, or depending (directly or not) on a PAUSED node."""
    seen: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current.status is ActionStatus.PAUSED:
            return True
        for dependency in current.depends_on:
            if dependency not in seen and dependency in by_id:
                seen.add(dependency)
                stack.append(by_id[dependency])
    return False


def cancel_unsubmitted(
    uow: UnitOfWork,
    step_up: StepUp,
    plan_id: str,
    at: datetime,
    *,
    reason: InvalidationReason,
    decision: str,
) -> list[ActionRow]:
    """Cancel every node not yet submitted; SUBMITTED and UNKNOWN nodes keep reconciling.

    Open challenges and grants of the plan are closed first. No unsubmitted node holds a
    reservation (reservations are made at submission), so none is released here.
    """
    k4.invalidate_grants(uow, step_up, reason, at, plan_id=plan_id)
    cancelled = []
    for node in plans.nodes(uow, plan_id):
        if node.status not in UNSUBMITTED:
            continue
        cancelled.append(actions.set_status(uow, node, ActionStatus.CANCELLED))
        _audit_node(uow, node, "node_cancelled", decision)
    return cancelled


def stop_plan(
    uow: UnitOfWork, step_up: StepUp, *, customer_id: str, plan_id: str, at: datetime
) -> PlanRow:
    """The customer stops a plan (SPEC §15.7): unsubmitted nodes are cancelled, plan partial.

    Stopping is risk-reducing, so it works under a lock. A final plan is returned unchanged.
    """
    plan = owned(plans.find_plan(uow, plan_id), customer_id, lambda p: p.customer_id)
    if plan.final:
        return plan
    cancel_unsubmitted(
        uow, step_up, plan_id, at, reason=InvalidationReason.PLAN_STOPPED, decision="stopped"
    )
    audit.record(uow, AuditActor.CUSTOMER, "plan_stopped", customer_id=customer_id, ref=plan_id)
    updated = plans.set_status(uow, plans.get_plan(uow, plan_id), PlanStatus.PARTIAL)
    _audit_plan(uow, plan, PlanStatus.PARTIAL)
    return updated


def _audit_node(uow: UnitOfWork, node: ActionRow, event: str, decision: str) -> None:
    audit.record(
        uow,
        AuditActor.COORDINATOR,
        event,
        customer_id=node.customer_id,
        ref=node.logical_action_id,
        decision=decision,
    )


def _audit_plan(uow: UnitOfWork, plan: PlanRow, status: PlanStatus) -> None:
    # A standalone action's own plan is bookkeeping; its action already has audit lines.
    if plan.kind is PlanKind.SINGLE:
        return
    audit.record(
        uow,
        AuditActor.COORDINATOR,
        "plan_status",
        customer_id=plan.customer_id,
        ref=plan.plan_id,
        decision=status.value,
    )
