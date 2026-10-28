"""Trusted customer steps: read and confirm cards, request and verify codes, the SMS panel.

Each step runs in one unit of work. A refusal raised inside the unit rolls it back; its
audit row is then written in a unit of its own, so every refusal leaves an audit line.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from wg.contracts.api import Card, ChallengeView, ConfirmResult, SmsMessage, SmsView, VerifyResult
from wg.contracts.enums import AuditActor
from wg.contracts.errors import ErrorClass, WgError
from wg.core import k4_approval as k4
from wg.core.k4_approval import Confirmation, Refusal, StepUp, Verified
from wg.k6 import actions, audit, challenges
from wg.k6.db import Database, UnitOfWork
from wg.k6.simtime import now


class TrustedService:
    def __init__(self, db: Database, step_up: StepUp) -> None:
        self.db = db
        self.step_up = step_up

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
            return k4.confirm_card(
                uow,
                self.step_up,
                customer_id=customer_id,
                card_id=card_id,
                presented_terms_hash=presented_terms_hash,
                now=now(uow),
            )

        with self._audited(customer_id, "confirm_refused", card_id):
            done = self.db.run_in_uow(run, "confirm card")
        action = done.action
        result = ConfirmResult(
            card_id=done.card.card_id,
            approval_id=done.approval.approval_id,
            plan_id=done.card.plan_id,
            logical_action_id=action.logical_action_id if action else None,
            action_status=action.status if action else None,
            challenge=k4.challenge_view(done.challenge) if done.challenge else None,
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
            outcome = self.db.run_in_uow(run, "verify code")
        if isinstance(outcome, Refusal):
            # The refusal and its audit row were committed with the attempt count.
            raise outcome.error()
        return VerifyResult(
            challenge_id=outcome.challenge.challenge_id,
            logical_action_id=outcome.action.logical_action_id,
            revision=outcome.action.revision,
            action_status=outcome.action.status,
        )

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


def record_refusal(db: Database, customer_id: str, event: str, ref: str, error: WgError) -> None:
    """One audit row for a refused customer request, in a unit of its own."""
    db.run_in_uow(
        lambda uow: audit.record(
            uow,
            AuditActor.CUSTOMER,
            event,
            customer_id=customer_id,
            ref=ref[:64],
            decision=error.code.value,
        ),
        "audit refusal",
    )
