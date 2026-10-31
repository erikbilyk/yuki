"""Trusted customer steps: read and confirm cards, request and verify codes, the SMS panel.

Each step runs in one unit of work. A refusal raised inside the unit rolls it back; its
audit row is then written in a unit of its own, so every refusal leaves an audit line.

Confirm and verify commit READY first and submit in a second unit. When that submission is
refused or fails, a third unit moves the action out of READY, so no later tick or restart
sends what the customer was told did not go through; only a new customer step does (SPEC §12.3).
Then the direct-debit sweep runs for the customer, so a payment or decline card that the
step made stale already shows expired when the response arrives (S5-SPEC §5.8).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from wg.contracts.api import (
    Card,
    CardKind,
    ChallengeView,
    ConfirmResult,
    OperationView,
    SmsMessage,
    SmsView,
    VerifyResult,
)
from wg.contracts.enums import ActionStatus, AuditActor, Tier
from wg.contracts.errors import ErrorClass, ErrorCode, WgError
from wg.core import k4_approval as k4
from wg.core import k5_mandates
from wg.core.k4_approval import Confirmation, InvalidationReason, Refusal, StepUp, Verified
from wg.core.k5_coordinator import Coordinator, store_status
from wg.k6 import actions, audit, challenges
from wg.k6.actions import ActionRow
from wg.k6.db import Database, UnitOfWork
from wg.k6.simtime import now
from wg.services.actions import ActionService
from wg.services.refusals import record_refusal

# A busy database retries under the same key, an integrity violation locks its scope and
# TIER_CHANGED has already moved the action to AWAITING_STEPUP; every other refusal or
# failure of the submission withdraws READY.
_WITHDRAWN_BY = frozenset({ErrorClass.EXPECTED, ErrorClass.UNEXPECTED})


class TrustedService:
    """The customer's own steps; a step that leaves the action READY submits it at once."""

    def __init__(
        self,
        db: Database,
        step_up: StepUp,
        action_service: ActionService,
        coordinator: Coordinator,
    ) -> None:
        self.db = db
        self.step_up = step_up
        self.action_service = action_service
        self.coordinator = coordinator

    def card(self, customer_id: str, card_id: str) -> Card:
        with self._audited(customer_id, "card_read_refused", card_id):
            with self.db.read() as conn:
                card = k4.find_owned_card(conn, customer_id, card_id)
                return k4.card_view(conn, card, now(conn))

    def confirm(
        self, customer_id: str, card_id: str, presented_terms_hash: str
    ) -> tuple[ConfirmResult, bool]:
        """The confirmation result and whether this request created the approval."""

        def run(uow: UnitOfWork) -> Confirmation:
            done = k4.confirm_card(
                uow,
                self.step_up,
                customer_id=customer_id,
                card_id=card_id,
                presented_terms_hash=presented_terms_hash,
                now=now(uow),
            )
            if done.card.kind is CardKind.PLAN and not done.repeated:
                self.coordinator.activate_plan(uow, done.card, done.approval)
            return done

        with self._audited(customer_id, "confirm_refused", card_id):
            done = self.db.run_in_uow(run, "confirm card")
            if done.card.kind is CardKind.PLAN and done.card.plan_id is not None:
                # Nodes that are due now move at once: a RED one waits for its code.
                self.coordinator.tick_plan(done.card.plan_id)
            action, operation = self._execute_and_sweep(customer_id, done.action)
        self.coordinator.continue_plans(customer_id)
        result = ConfirmResult(
            card_id=done.card.card_id,
            approval_id=done.approval.approval_id,
            plan_id=done.card.plan_id,
            logical_action_id=action.logical_action_id if action else None,
            action_status=action.status if action else None,
            challenge=k4.challenge_view(done.challenge) if done.challenge else None,
            operation=operation,
        )
        return result, not done.repeated

    def request_challenge(self, customer_id: str, logical_action_id: str) -> ChallengeView:
        return self._issue(
            "challenge_refused",
            logical_action_id,
            customer_id,
            lambda uow: k4.request_challenge(
                uow,
                self.step_up,
                customer_id=customer_id,
                logical_action_id=logical_action_id,
                now=now(uow),
            ),
        )

    def reissue(self, customer_id: str, challenge_id: str) -> ChallengeView:
        return self._issue(
            "reissue_refused",
            challenge_id,
            customer_id,
            lambda uow: k4.reissue_challenge(
                uow, self.step_up, customer_id=customer_id, challenge_id=challenge_id, now=now(uow)
            ),
        )

    def verify(self, customer_id: str, challenge_id: str, code: str) -> VerifyResult:
        def run(uow: UnitOfWork) -> Verified | Refusal:
            return k4.verify_code(
                uow,
                self.step_up,
                customer_id=customer_id,
                challenge_id=challenge_id,
                code=code,
                now=now(uow),
            )

        with self._audited(customer_id, "verify_refused", challenge_id):
            outcome = self.db.run_in_uow(
                run, "verify code", plan_id=self._plan_of_challenge(challenge_id)
            )
        if isinstance(outcome, Refusal):
            # The refusal and its audit row were committed with the attempt count.
            raise outcome.error()
        with self._audited(customer_id, "verify_refused", challenge_id):
            action, operation = self._execute_and_sweep(customer_id, outcome.action)
        self.coordinator.continue_plans(customer_id)
        return VerifyResult(
            challenge_id=outcome.challenge.challenge_id,
            logical_action_id=action.logical_action_id,
            revision=action.revision,
            action_status=action.status,
            operation=operation,
        )

    def _plan_of_challenge(self, challenge_id: str) -> str | None:
        """The plan a verification failure is recorded against (SPEC §16)."""
        with self.db.read() as conn:
            challenge = challenges.find_challenge(conn, challenge_id)
            if challenge is None:
                return None
            action = actions.get_revision(conn, challenge.logical_action_id, challenge.revision)
            return action.plan_id

    def sms(self, customer_id: str) -> SmsView:
        """Open codes of the customer's simulated phone; expired and closed ones never show."""
        with self.db.read() as conn:
            at = now(conn)
            messages = []
            for row in challenges.open_sms(conn, customer_id, at):
                challenge = challenges.get_challenge(conn, row.challenge_id)
                action = actions.get_revision(conn, challenge.logical_action_id, challenge.revision)
                amount = action.params.get("amount_fen")
                messages.append(
                    SmsMessage(
                        challenge_id=row.challenge_id,
                        logical_action_id=challenge.logical_action_id,
                        kind=action.kind,
                        amount_fen=amount if isinstance(amount, int) else None,
                        code=row.code,
                        created_at=row.created_at,
                        expires_at=challenge.expires_at,
                    )
                )
        return SmsView(messages=tuple(messages))

    def _execute_and_sweep(
        self, customer_id: str, action: ActionRow | None
    ) -> tuple[ActionRow | None, OperationView | None]:
        """Submit as `_execute` does; then, also after a refusal, sweep the customer's records."""
        try:
            return self._execute(customer_id, action)
        finally:
            k5_mandates.sweep(self.db, self.step_up, customer_id=customer_id)

    def _execute(
        self, customer_id: str, action: ActionRow | None
    ) -> tuple[ActionRow | None, OperationView | None]:
        """Submit a READY action; return the action as it stands afterwards and its operation."""
        try:
            operation = self.action_service.execute(customer_id, action)
        except WgError as error:
            if action is not None and _withdraws(error):
                self._withdraw_ready(action, error.code)
            raise
        if action is None:
            return None, None
        with self.db.read() as conn:
            return actions.get_revision(conn, action.logical_action_id, action.revision), operation

    def _withdraw_ready(self, action: ActionRow, code: ErrorCode) -> None:
        """After a refused submission the action waits for a new customer step (SPEC §12.3).

        An action with a grant, or a RED one, loses the grant and waits for a new code. Any
        other goes back to AWAITING_APPROVAL; confirming its card again makes it READY.
        """

        def run(uow: UnitOfWork) -> None:
            current = actions.get_revision(uow, action.logical_action_id, action.revision)
            if current.status is not ActionStatus.READY:
                return
            if current.tier is Tier.RED or k4.find_grant(uow, current) is not None:
                k4.invalidate_grants(
                    uow,
                    self.step_up,
                    InvalidationReason.SUBMISSION_REFUSED,
                    now(uow),
                    logical_action_id=current.logical_action_id,
                )
                status = ActionStatus.AWAITING_STEPUP
            else:
                status = ActionStatus.AWAITING_APPROVAL
            current = actions.get_revision(uow, current.logical_action_id, current.revision)
            if current.status is ActionStatus.READY:
                actions.set_status(uow, current, status)
            audit.record(
                uow,
                AuditActor.SYSTEM,
                "submission_withdrawn",
                customer_id=current.customer_id,
                ref=current.logical_action_id,
                decision=status.value,
                reason=code.value,
            )
            if current.plan_id is not None:
                store_status(uow, current.plan_id)

        self.db.run_in_uow(
            run,
            "withdraw ready",
            plan_id=action.plan_id,
            logical_action_id=action.logical_action_id,
        )

    def _issue(
        self,
        event: str,
        ref: str,
        customer_id: str,
        fn: Callable[[UnitOfWork], challenges.ChallengeRow],
    ) -> ChallengeView:
        with self._audited(customer_id, event, ref):
            challenge = self.db.run_in_uow(fn, event.removesuffix("_refused"))
        return k4.challenge_view(challenge)

    @contextmanager
    def _audited(self, customer_id: str, event: str, ref: str) -> Iterator[None]:
        """Write an audit row for an expected refusal raised in the block, then re-raise it."""
        try:
            yield
        except WgError as error:
            if error.error_class is ErrorClass.EXPECTED:
                record_refusal(self.db, customer_id, event, ref, error)
            raise


def _withdraws(error: WgError) -> bool:
    return error.error_class in _WITHDRAWN_BY and error.code is not ErrorCode.TIER_CHANGED
