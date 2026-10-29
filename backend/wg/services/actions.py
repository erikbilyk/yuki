"""Drafts, approval requests, cancellation and execution of single actions (SPEC §9, §12).

The guided UI and, later, the agent tools call the same methods. A draft grants nothing.
Every slot of a draft carries its provenance: a payee or an amount read from untrusted text
is refused with UNTRUSTED_SOURCE, and a remark is stored as opaque untrusted text, never
read as an instruction (SPEC §18.3, T10).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue

from wg.contracts.api import ApprovalRequestResult, CancelResult, DraftResult, OperationView
from wg.contracts.enums import ActionKind, ActionStatus, AuditActor, Provenance
from wg.contracts.errors import ErrorClass, ErrorCode, WgError
from wg.contracts.ids import IdPrefix, new_id
from wg.core import k4_approval as k4
from wg.core.k1_session import not_found, owned
from wg.core.k3_policy import check_not_locked
from wg.core.k4_approval import InvalidationReason, StepUp
from wg.core.k5_coordinator import store_status
from wg.k6 import accounts, actions, audit, operations
from wg.k6.actions import ActionRow
from wg.k6.db import Database, UnitOfWork
from wg.k6.gateway import Gateway
from wg.k6.simtime import now
from wg.packs.base import Infeasible
from wg.packs.registry import pack_for
from wg.services.refusals import record_refusal
from wg.services.views import operation_view

# Steps an unsubmitted action can be cancelled from (SPEC §8.1).
CANCELLABLE = frozenset(
    {
        ActionStatus.DRAFT,
        ActionStatus.AWAITING_APPROVAL,
        ActionStatus.AWAITING_STEPUP,
        ActionStatus.READY,
        ActionStatus.PAUSED,
    }
)
EXECUTED = frozenset(
    {ActionStatus.SUBMITTED, ActionStatus.SETTLED, ActionStatus.UNKNOWN, ActionStatus.REJECTED}
)


@dataclass(frozen=True)
class Slot[T]:
    """A value with where it came from and, for the agent, the fragment it was read from."""

    value: T
    provenance: Provenance
    quote: str | None = None

    def record(self) -> dict[str, JsonValue]:
        entry: dict[str, JsonValue] = {"provenance": self.provenance.value}
        if self.quote is not None:
            entry["quote"] = self.quote
        return entry


@dataclass(frozen=True)
class TransferDraft:
    payee: Slot[str]
    amount: Slot[int]
    account_id: str | None = None
    remark: str | None = None

    def untrusted_slots(self) -> list[str]:
        slots = {"payee_id": self.payee, "amount_fen": self.amount}
        return [
            name for name, slot in slots.items() if slot.provenance is Provenance.UNTRUSTED_TEXT
        ]


class ActionService:
    def __init__(self, db: Database, step_up: StepUp, gateway: Gateway) -> None:
        self.db = db
        self.step_up = step_up
        self.gateway = gateway

    # Drafts (SPEC §9.1)

    def draft_transfer(
        self, customer_id: str, draft: TransferDraft, actor: AuditActor = AuditActor.CUSTOMER
    ) -> DraftResult:
        """A TRANSFER in DRAFT with its tier and feasibility; an infeasible draft lists why."""
        with self._audited(customer_id, "draft_refused", draft.payee.value, actor):
            untrusted = draft.untrusted_slots()
            if untrusted:
                raise WgError(ErrorCode.UNTRUSTED_SOURCE, {"slots": list(untrusted)})
            action, reasons = self.db.run_in_uow(
                lambda uow: self._insert_transfer(uow, customer_id, draft, actor), "draft transfer"
            )
        return _draft_result(action, reasons)

    def _insert_transfer(
        self, uow: UnitOfWork, customer_id: str, draft: TransferDraft, actor: AuditActor
    ) -> tuple[ActionRow, tuple[Infeasible, ...]]:
        at = now(uow)
        payee = owned(accounts.find_payee(uow, draft.payee.value), customer_id, _owner)
        if not payee.active:
            raise not_found()
        if draft.account_id is None:
            account = accounts.main_account(uow, customer_id)
            if account is None:
                raise not_found()
        else:
            account = owned(accounts.find_account(uow, draft.account_id), customer_id, _owner)
        kind = ActionKind.TRANSFER
        pack = pack_for(kind)
        params = pack.canonical(
            kind,
            {
                "account_id": account.account_id,
                "payee_id": payee.payee_id,
                "amount_fen": draft.amount.value,
                "remark": draft.remark,
            },
        )
        reasons = pack.precheck(uow, customer_id, kind, params, at)
        for reason in reasons:
            if reason.code is ErrorCode.SINGLE_LIMIT_EXCEEDED:
                raise reason.error()
        provenance: dict[str, Any] = {
            "payee_id": draft.payee.record(),
            "amount_fen": draft.amount.record(),
            # The main account is the bank's default; a chosen account is the customer's choice.
            "account_id": {
                "provenance": (
                    Provenance.SERVER_RECORD
                    if draft.account_id is None
                    else Provenance.USER_UTTERANCE
                ).value
            },
        }
        if "remark" in params:
            provenance["remark"] = {"provenance": Provenance.UNTRUSTED_TEXT.value}
        action = actions.insert_action(
            uow,
            logical_action_id=new_id(IdPrefix.ACTION),
            customer_id=customer_id,
            kind=kind,
            params=params,
            request_hash=pack.request_hash(kind, customer_id, params),
            tier=pack.tier(uow, customer_id, kind, params, at),
            provenance=provenance,
        )
        audit.record(
            uow,
            actor,
            "draft_created",
            customer_id=customer_id,
            ref=action.logical_action_id,
            decision=action.tier.value,
            reason=",".join(r.code.value for r in reasons)[:64] or None,
        )
        return action, reasons

    # Approval request and cancellation

    def request_approval(
        self, customer_id: str, logical_action_id: str, actor: AuditActor = AuditActor.CUSTOMER
    ) -> ApprovalRequestResult:
        """Show the action on a trusted card; tier and feasibility are checked again first."""

        def run(uow: UnitOfWork) -> tuple[ActionRow, str]:
            at = now(uow)
            action = owned(actions.find_current(uow, logical_action_id), customer_id, _owner)
            if action.status not in (ActionStatus.DRAFT, ActionStatus.AWAITING_APPROVAL):
                raise k4.state_refusal(action).error()
            pack = pack_for(action.kind)
            if action.status is ActionStatus.DRAFT:
                check_not_locked(uow, customer_id, action.plan_id)
                reasons = pack.precheck(uow, customer_id, action.kind, action.params, at)
                if reasons:
                    detail = {**reasons[0].detail, "reasons": [r.code.value for r in reasons]}
                    raise WgError(reasons[0].code, detail)
                tier = pack.tier(uow, customer_id, action.kind, action.params, at)
                if tier is not action.tier:
                    action = actions.set_tier(uow, action, tier)
            card = k4.open_action_card(uow, action, pack.card(uow, action, at), at)
            return actions.get_revision(
                uow, action.logical_action_id, action.revision
            ), card.card_id

        with self._audited(customer_id, "approval_request_refused", logical_action_id, actor):
            action, card_id = self.db.run_in_uow(run, "request approval")
        return ApprovalRequestResult(
            logical_action_id=action.logical_action_id,
            revision=action.revision,
            status=action.status,
            tier=action.tier,
            card_id=card_id,
        )

    def cancel(
        self, customer_id: str, logical_action_id: str, actor: AuditActor = AuditActor.CUSTOMER
    ) -> CancelResult:
        """Cancel an action that has not been submitted; a settled one is never undone."""

        def run(uow: UnitOfWork) -> ActionRow:
            action = owned(actions.find_current(uow, logical_action_id), customer_id, _owner)
            if action.status is ActionStatus.CANCELLED:
                return action
            if action.status not in CANCELLABLE:
                raise k4.state_refusal(action).error()
            # Cancelling reduces risk, so it is allowed under a lock (SPEC §16).
            k4.invalidate_grants(
                uow,
                self.step_up,
                InvalidationReason.CANCELLED,
                now(uow),
                logical_action_id=logical_action_id,
            )
            cancelled = actions.set_status(uow, action, ActionStatus.CANCELLED)
            audit.record(
                uow, actor, "action_cancelled", customer_id=customer_id, ref=logical_action_id
            )
            if cancelled.plan_id is not None:
                store_status(uow, cancelled.plan_id)
            return cancelled

        with self._audited(customer_id, "cancel_refused", logical_action_id, actor):
            action = self.db.run_in_uow(run, "cancel action")
        # Only a submitted operation holds a reservation, and a submitted action is never
        # cancelled, so cancelling releases none.
        return CancelResult(
            logical_action_id=action.logical_action_id,
            revision=action.revision,
            status=action.status,
            reservation_released=False,
        )

    # Execution

    def execute(self, customer_id: str, action: ActionRow | None) -> OperationView | None:
        """Submit a READY action through the gateway, or show the operation it already has."""
        if action is None:
            return None
        if action.status is ActionStatus.READY:
            operation = self.gateway.submit(customer_id, action.logical_action_id).operation
        elif action.status in EXECUTED:
            key = operations.idempotency_key(action.logical_action_id, action.revision)
            with self.db.read() as conn:
                found = operations.find_by_key(conn, key)
            if found is None:
                return None
            operation = found
        else:
            return None
        with self.db.read() as conn:
            return operation_view(conn, operation)

    @contextmanager
    def _audited(self, customer_id: str, event: str, ref: str, actor: AuditActor) -> Iterator[None]:
        try:
            yield
        except WgError as error:
            if error.error_class is ErrorClass.EXPECTED:
                record_refusal(self.db, customer_id, event, ref, error, actor=actor)
            raise


def _owner(record: Any) -> str | None:
    return record.customer_id


def _draft_result(action: ActionRow, reasons: tuple[Infeasible, ...]) -> DraftResult:
    return DraftResult(
        logical_action_id=action.logical_action_id,
        revision=action.revision,
        kind=action.kind,
        status=action.status,
        tier=action.tier,
        feasible=not reasons,
        reasons=tuple(r.code for r in reasons),
    )
