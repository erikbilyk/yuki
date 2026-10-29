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

from wg.contracts.api import (
    ApprovalRequestResult,
    BillDraftRequest,
    CancelResult,
    DraftResult,
    OperationView,
)
from wg.contracts.enums import ActionKind, ActionStatus, AuditActor, CommitmentStatus, Provenance
from wg.contracts.errors import ErrorClass, ErrorCode, WgError
from wg.contracts.ids import IdPrefix, new_id
from wg.core import k4_approval as k4
from wg.core.k1_session import not_found, owned
from wg.core.k3_policy import check_not_locked, is_risk_reducing
from wg.core.k4_approval import InvalidationReason, StepUp
from wg.core.k5_coordinator import store_status
from wg.k6 import accounts, actions, audit, commitments, imports, operations
from wg.k6.actions import ActionRow
from wg.k6.categories import CATEGORIES
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


@dataclass(frozen=True)
class PayBillDraft:
    """A bill payment: `amount` None pays the whole unpaid amount."""

    commitment: Slot[str]
    amount: Slot[int] | None = None
    account_id: str | None = None

    def untrusted_slots(self) -> list[str]:
        slots = {"commitment_id": self.commitment, "amount_fen": self.amount}
        return [
            name
            for name, slot in slots.items()
            if slot is not None and slot.provenance is Provenance.UNTRUSTED_TEXT
        ]


# Refusals that end a draft at once; every other reason is listed on the draft (T8, T25).
_REFUSED_AT_DRAFT = frozenset(
    {
        ErrorCode.SINGLE_LIMIT_EXCEEDED,
        ErrorCode.BILL_NOT_PAYABLE,
        ErrorCode.AMOUNT_OVER_UNPAID,
        ErrorCode.PAYEE_MISMATCH,
    }
)

BILL_SOURCE_CUSTOMER = "customer"


def _account_provenance(account_id: str | None) -> dict[str, JsonValue]:
    """The main account is the bank's default; a chosen account is the customer's choice."""
    chosen = Provenance.SERVER_RECORD if account_id is None else Provenance.USER_UTTERANCE
    return {"provenance": chosen.value}


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

    def draft_pay_bill(
        self, customer_id: str, draft: PayBillDraft, actor: AuditActor = AuditActor.CUSTOMER
    ) -> DraftResult:
        """A PAY_BILL for a bill the customer owes: the bill's payee, at most its unpaid amount."""
        with self._audited(customer_id, "draft_refused", draft.commitment.value, actor):
            untrusted = draft.untrusted_slots()
            if untrusted:
                raise WgError(ErrorCode.UNTRUSTED_SOURCE, {"slots": list(untrusted)})
            action, reasons = self.db.run_in_uow(
                lambda uow: self._insert_pay_bill(uow, customer_id, draft, actor), "draft pay bill"
            )
        return _draft_result(action, reasons, action.params["commitment_id"])

    def _insert_pay_bill(
        self, uow: UnitOfWork, customer_id: str, draft: PayBillDraft, actor: AuditActor
    ) -> tuple[ActionRow, tuple[Infeasible, ...]]:
        at = now(uow)
        check_not_locked(uow, customer_id, None)
        bill = owned(commitments.find_commitment(uow, draft.commitment.value), customer_id, _owner)
        payee = accounts.find_payee(uow, bill.payee_id)
        if payee is None or not payee.active:
            raise not_found()
        account = self._debit_account(uow, customer_id, draft.account_id)
        if bill.status not in commitments.OWED:
            raise WgError(ErrorCode.BILL_NOT_PAYABLE, {"status": bill.status.value})
        amount = bill.unpaid_fen if draft.amount is None else draft.amount.value
        kind = ActionKind.PAY_BILL
        pack = pack_for(kind)
        params = pack.canonical(
            kind,
            {
                "account_id": account.account_id,
                "payee_id": payee.payee_id,
                "amount_fen": amount,
                "commitment_id": bill.commitment_id,
            },
        )
        reasons = pack.precheck(uow, customer_id, kind, params, at)
        for reason in reasons:
            if reason.code in _REFUSED_AT_DRAFT:
                raise reason.error()
        provenance = {
            "commitment_id": draft.commitment.record(),
            "payee_id": {"provenance": Provenance.SERVER_RECORD.value},
            "amount_fen": (
                {"provenance": Provenance.SERVER_RECORD.value}
                if draft.amount is None
                else draft.amount.record()
            ),
            "account_id": _account_provenance(draft.account_id),
        }
        return self._insert_draft(
            uow, customer_id, kind, params, provenance, reasons, actor
        ), reasons

    def _debit_account(
        self, uow: UnitOfWork, customer_id: str, account_id: str | None
    ) -> accounts.AccountRow:
        if account_id is None:
            account = accounts.main_account(uow, customer_id)
            if account is None:
                raise not_found()
            return account
        return owned(accounts.find_account(uow, account_id), customer_id, _owner)

    def draft_bill(
        self, customer_id: str, request: BillDraftRequest, actor: AuditActor = AuditActor.CUSTOMER
    ) -> DraftResult:
        """CONFIRM_BILL for a candidate, or for a bill the customer typed in whole."""
        ref = request.commitment_id or request.payee_id or ""
        with self._audited(customer_id, "draft_refused", ref, actor):
            action, reasons = self.db.run_in_uow(
                lambda uow: self._insert_bill_confirmation(uow, customer_id, request, actor),
                "draft bill",
            )
        return _draft_result(action, reasons, action.params["commitment_id"])

    def _insert_bill_confirmation(
        self, uow: UnitOfWork, customer_id: str, request: BillDraftRequest, actor: AuditActor
    ) -> tuple[ActionRow, tuple[Infeasible, ...]]:
        check_not_locked(uow, customer_id, None)
        if request.commitment_id is not None:
            bill = owned(
                commitments.find_commitment(uow, request.commitment_id), customer_id, _owner
            )
            if bill.status is not CommitmentStatus.CANDIDATE:
                raise WgError(ErrorCode.UNSUPPORTED_REQUEST, {"reason": "not_a_candidate"})
            provenance = {"commitment_id": {"provenance": Provenance.USER_UTTERANCE.value}}
        else:
            assert request.payee_id and request.purpose and request.amount_fen and request.due_date
            payee = owned(accounts.find_payee(uow, request.payee_id), customer_id, _owner)
            if not payee.active:
                raise not_found()
            found = commitments.find_duplicate_candidate(
                uow,
                customer_id,
                payee.payee_id,
                request.purpose,
                request.amount_fen,
                request.due_date,
            )
            if found is not None and found.status is not CommitmentStatus.CANDIDATE:
                raise WgError(ErrorCode.DUPLICATE_INTENT, {"commitment_id": found.commitment_id})
            bill = found or commitments.insert_candidate(
                uow,
                customer_id=customer_id,
                payee_id=payee.payee_id,
                kind=request.purpose,
                amount_fen=request.amount_fen,
                due_date=request.due_date,
                source=BILL_SOURCE_CUSTOMER,
            )
            provenance = {
                slot: {"provenance": Provenance.USER_UTTERANCE.value}
                for slot in ("commitment_id", "payee_id", "amount_fen", "due_date")
            }
        return self._draft_bill_action(
            uow, customer_id, ActionKind.CONFIRM_BILL, bill, provenance, actor
        )

    def draft_bill_action(
        self,
        customer_id: str,
        kind: ActionKind,
        commitment_id: str,
        actor: AuditActor = AuditActor.CUSTOMER,
    ) -> DraftResult:
        """CANCEL_BILL or MARK_PAID_EXTERNAL for a bill the customer owes."""

        def build(uow: UnitOfWork) -> tuple[ActionRow, tuple[Infeasible, ...]]:
            check_not_locked(uow, customer_id, None)
            bill = owned(commitments.find_commitment(uow, commitment_id), customer_id, _owner)
            provenance = {"commitment_id": {"provenance": Provenance.USER_UTTERANCE.value}}
            return self._draft_bill_action(uow, customer_id, kind, bill, provenance, actor)

        with self._audited(customer_id, "draft_refused", commitment_id, actor):
            action, reasons = self.db.run_in_uow(build, f"draft {kind.value.lower()}")
        return _draft_result(action, reasons, commitment_id)

    def _draft_bill_action(
        self,
        uow: UnitOfWork,
        customer_id: str,
        kind: ActionKind,
        bill: commitments.CommitmentRow,
        provenance: dict[str, Any],
        actor: AuditActor,
    ) -> tuple[ActionRow, tuple[Infeasible, ...]]:
        pack = pack_for(kind)
        params = pack.canonical(kind, {"commitment_id": bill.commitment_id})
        reasons = pack.precheck(uow, customer_id, kind, params, now(uow))
        action = self._insert_draft(uow, customer_id, kind, params, provenance, reasons, actor)
        return action, reasons

    def draft_reserve(
        self, customer_id: str, target_fen: int, actor: AuditActor = AuditActor.CUSTOMER
    ) -> DraftResult:
        """DESIGNATE_RESERVE: YELLOW to raise the target (or keep it), RED to lower it."""

        def build(uow: UnitOfWork) -> tuple[ActionRow, tuple[Infeasible, ...]]:
            kind = ActionKind.DESIGNATE_RESERVE
            pack = pack_for(kind)
            params = pack.canonical(kind, {"target_fen": target_fen})
            check_not_locked(
                uow,
                customer_id,
                None,
                risk_reducing=is_risk_reducing(uow, customer_id, kind, params),
            )
            provenance = {"target_fen": {"provenance": Provenance.USER_UTTERANCE.value}}
            action = self._insert_draft(uow, customer_id, kind, params, provenance, (), actor)
            return action, ()

        with self._audited(customer_id, "draft_refused", "reserve", actor):
            action, reasons = self.db.run_in_uow(build, "draft reserve")
        return _draft_result(action, reasons)

    def draft_resolution(
        self,
        customer_id: str,
        txn_id: str,
        category: str,
        actor: AuditActor = AuditActor.CUSTOMER,
    ) -> DraftResult:
        """RESOLVE_ITEM: the customer's class for a statement line nothing else classified."""

        def build(uow: UnitOfWork) -> tuple[ActionRow, tuple[Infeasible, ...]]:
            check_not_locked(uow, customer_id, None)
            line = imports.find_line(uow, customer_id, txn_id)
            if line is None:
                raise not_found()
            if category not in CATEGORIES:
                raise WgError(ErrorCode.UNSUPPORTED_REQUEST, {"fields": ["body.category"]})
            kind = ActionKind.RESOLVE_ITEM
            pack = pack_for(kind)
            params = pack.canonical(
                kind, {"account_id": line.account_id, "txn_id": txn_id, "category": category}
            )
            reasons = pack.precheck(uow, customer_id, kind, params, now(uow))
            provenance = {
                "txn_id": {"provenance": Provenance.SERVER_RECORD.value},
                "category": {"provenance": Provenance.USER_UTTERANCE.value},
            }
            return self._insert_draft(
                uow, customer_id, kind, params, provenance, reasons, actor
            ), reasons

        with self._audited(customer_id, "draft_refused", txn_id, actor):
            action, reasons = self.db.run_in_uow(build, "draft resolution")
        return _draft_result(action, reasons)

    def _insert_transfer(
        self, uow: UnitOfWork, customer_id: str, draft: TransferDraft, actor: AuditActor
    ) -> tuple[ActionRow, tuple[Infeasible, ...]]:
        at = now(uow)
        check_not_locked(uow, customer_id, None)
        payee = owned(accounts.find_payee(uow, draft.payee.value), customer_id, _owner)
        if not payee.active:
            raise not_found()
        account = self._debit_account(uow, customer_id, draft.account_id)
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
            "account_id": _account_provenance(draft.account_id),
        }
        if "remark" in params:
            provenance["remark"] = {"provenance": Provenance.UNTRUSTED_TEXT.value}
        return self._insert_draft(
            uow, customer_id, kind, params, provenance, reasons, actor
        ), reasons

    def _insert_draft(
        self,
        uow: UnitOfWork,
        customer_id: str,
        kind: ActionKind,
        params: dict[str, Any],
        provenance: dict[str, Any],
        reasons: tuple[Infeasible, ...],
        actor: AuditActor,
    ) -> ActionRow:
        """The DRAFT row with its tier and an audit line; a draft grants nothing."""
        at = now(uow)
        pack = pack_for(kind)
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
        return action

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
                check_not_locked(
                    uow,
                    customer_id,
                    action.plan_id,
                    risk_reducing=is_risk_reducing(uow, customer_id, action.kind, action.params),
                )
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
            if operation is None:
                return None
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


def _draft_result(
    action: ActionRow, reasons: tuple[Infeasible, ...], commitment_id: str | None = None
) -> DraftResult:
    return DraftResult(
        logical_action_id=action.logical_action_id,
        revision=action.revision,
        kind=action.kind,
        status=action.status,
        tier=action.tier,
        feasible=not reasons,
        reasons=tuple(r.code for r in reasons),
        commitment_id=commitment_id,
    )
