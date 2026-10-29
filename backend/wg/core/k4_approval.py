"""K4: approval cards, approvals, step-up challenges and grants (SPEC §5.4, §9).

Every function here runs inside the caller's unit of work and takes the simulated time
`now` read in that unit. A refusal whose side effects must persist (a counted attempt, an
invalidated challenge and their audit rows) is returned as a `Refusal` value, so the unit
commits and the caller raises it afterwards. Other refusals are raised and roll back.

Nothing here is reachable from the agent: approvals and challenges come only from the
customer's trusted routes, and grants are consumed only by the gateway.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import JsonValue

from wg.contracts.api import (
    Card,
    CardChange,
    CardKind,
    CardLine,
    CardStatus,
    ChallengeView,
    SourceLabel,
)
from wg.contracts.enums import ActionStatus, ApprovalScopeKind, AuditActor, PlanKind, Tier
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.ids import IdPrefix, new_id
from wg.core.hashes import hmac_hex, sha256_hex
from wg.core.k1_session import not_found, owned
from wg.core.k3_policy import check_not_locked, day_bounds
from wg.k6 import actions, approvals, audit, challenges, plans
from wg.k6.actions import ActionRow
from wg.k6.approvals import ApprovalRow, CardRow
from wg.k6.challenges import ChallengeRow
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import SHANGHAI, to_db

CODE_TTL = timedelta(minutes=5)
MAX_ATTEMPTS = 3
CODE_DIGITS = 6


class InvalidationReason(StrEnum):
    EXPIRED = "expired"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    REISSUED = "reissued"
    NEW_REVISION = "new_revision"
    PLAN_STOPPED = "plan_stopped"
    LOCK_CLEARED = "lock_cleared"
    APPROVAL_REVOKED = "approval_revoked"
    APPROVAL_EXPIRED = "approval_expired"
    SCOPE_MISMATCH = "scope_mismatch"
    CANCELLED = "cancelled"


# The refusal a later verify of an invalidated challenge gets.
_REFUSAL_AFTER: dict[str, ErrorCode] = {
    InvalidationReason.EXPIRED: ErrorCode.CODE_EXPIRED,
    InvalidationReason.ATTEMPTS_EXHAUSTED: ErrorCode.CODE_ATTEMPTS_EXHAUSTED,
    InvalidationReason.NEW_REVISION: ErrorCode.SUPERSEDED,
    InvalidationReason.APPROVAL_REVOKED: ErrorCode.APPROVAL_REVOKED,
    InvalidationReason.APPROVAL_EXPIRED: ErrorCode.APPROVAL_EXPIRED,
    InvalidationReason.SCOPE_MISMATCH: ErrorCode.CODE_SCOPE_MISMATCH,
}

# Invalidating a grant for these reasons returns a READY action to AWAITING_STEPUP, so it
# waits for a new code (SPEC §8.1, §16). For the other reasons the caller moves the action.
_BACK_TO_STEPUP = frozenset(
    {
        InvalidationReason.EXPIRED,
        InvalidationReason.LOCK_CLEARED,
        InvalidationReason.APPROVAL_REVOKED,
        InvalidationReason.APPROVAL_EXPIRED,
    }
)


@dataclass(frozen=True)
class Refusal:
    """An expected refusal whose state changes are committed before it is raised."""

    code: ErrorCode
    detail: dict[str, JsonValue] = field(default_factory=dict)

    def error(self) -> WgError:
        return WgError(self.code, self.detail)


# Verification provider (SPEC §9.3)


class VerificationProvider(Protocol):
    """Delivers a step-up code to the customer and withdraws it when the challenge closes."""

    def deliver(self, uow: UnitOfWork, customer_id: str, challenge_id: str, code: str) -> None: ...

    def withdraw(self, uow: UnitOfWork, challenge_id: str) -> None: ...


class SimulatedSms:
    """The simulated phone: the code is kept in `sms_outbox` only while the challenge is open."""

    def deliver(self, uow: UnitOfWork, customer_id: str, challenge_id: str, code: str) -> None:
        challenges.insert_sms(uow, customer_id, challenge_id, code)

    def withdraw(self, uow: UnitOfWork, challenge_id: str) -> None:
        challenges.wipe_code(uow, challenge_id)


@dataclass(frozen=True)
class StepUp:
    """What issuing and checking codes needs: the HMAC key and the delivery channel."""

    code_key: str = field(repr=False)
    provider: VerificationProvider

    def __post_init__(self) -> None:
        if not self.code_key:
            raise ValueError("the code HMAC key is empty")

    def code_hmac(self, challenge_id: str, scope_hash: str, code: str) -> str:
        return hmac_hex(self.code_key, f"{challenge_id}:{scope_hash}:{code}")


# Terms and hashes (SPEC §5.4)


@dataclass(frozen=True)
class CardContent:
    """What the trusted UI shows on a card besides its terms; rendered by the pack."""

    title: str
    lines: tuple[CardLine, ...]
    changes: tuple[CardChange, ...] = ()
    sources: tuple[SourceLabel, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "lines": [line.model_dump(mode="json") for line in self.lines],
            "changes": [change.model_dump(mode="json") for change in self.changes],
            "sources": [source.model_dump(mode="json") for source in self.sources],
            "warnings": list(self.warnings),
        }


def action_terms(action: ActionRow, expires_at: datetime) -> dict[str, Any]:
    """The canonical terms of a single action at one revision."""
    return {
        "scope_kind": ApprovalScopeKind.ACTION.value,
        "customer_id": action.customer_id,
        "plan_id": action.plan_id,
        "logical_action_id": action.logical_action_id,
        "revision": action.revision,
        "kind": action.kind.value,
        "tier": action.tier.value,
        "params": dict(action.params),
        "request_hash": action.request_hash,
        "expires_at": to_db(expires_at),
    }


def terms_hash(terms: Mapping[str, Any]) -> str:
    return sha256_hex(dict(terms))


def scope_hash(action: ActionRow, approval_terms_hash: str) -> str:
    """Binds a code to one effect: who, which action and revision, from where, to whom, how much.

    Packs name these params `account_id`, `payee_id` or `product_id`, and `amount_fen` or
    `shares`; a kind without one of them binds None in its place.
    """
    params = action.params
    return sha256_hex(
        {
            "customer_id": action.customer_id,
            "logical_action_id": action.logical_action_id,
            "revision": action.revision,
            "account_id": params.get("account_id"),
            "counterparty": params.get("payee_id") or params.get("product_id"),
            "amount_fen": params.get("amount_fen"),
            "shares": params.get("shares"),
            "terms_hash": approval_terms_hash,
        }
    )


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# Cards


def action_card_expiry(now: datetime) -> datetime:
    """A single-action card and its approval hold until the end of the simulated day.

    The card shows the tier and the rest of the day's limit, which are facts of that day.
    """
    return day_bounds(now.astimezone(SHANGHAI).date())[1]


def card_status(reader: Reader, card: CardRow, now: datetime) -> CardStatus:
    if card.approval_id is not None:
        return CardStatus.CONFIRMED
    if card.logical_action_id is not None and card.revision is not None:
        action = actions.get_revision(reader, card.logical_action_id, card.revision)
        if action.status is ActionStatus.SUPERSEDED:
            return CardStatus.SUPERSEDED
        if card.kind is CardKind.ACTION and action.status is not ActionStatus.AWAITING_APPROVAL:
            return CardStatus.EXPIRED
    if now >= card.expires_at:
        return CardStatus.EXPIRED
    return CardStatus.OPEN


def open_action_card(
    uow: UnitOfWork,
    action: ActionRow,
    content: CardContent,
    now: datetime,
    *,
    cash_snapshot_id: str | None = None,
    calc_result_id: str | None = None,
) -> CardRow:
    """Show a single action on a trusted card: DRAFT moves to AWAITING_APPROVAL.

    Asking again while the card is open returns the same card; after it expired, a new one.
    """
    if action.tier is Tier.FORBIDDEN:
        raise ValueError("a FORBIDDEN action never gets a card")
    if action.status is ActionStatus.AWAITING_APPROVAL:
        shown = approvals.cards_of_action(uow, action.logical_action_id, action.revision)
        for card in reversed(shown):
            if card_status(uow, card, now) is CardStatus.OPEN:
                return card
    elif action.status is ActionStatus.DRAFT:
        if action.plan_id is None:
            action = plans.attach_single(uow, action)
        action = actions.set_status(uow, action, ActionStatus.AWAITING_APPROVAL)
    else:
        raise state_refusal(action).error()
    expires_at = action_card_expiry(now)
    terms = action_terms(action, expires_at)
    card = approvals.insert_card(
        uow,
        card_id=new_id(IdPrefix.CARD),
        customer_id=action.customer_id,
        kind=CardKind.ACTION,
        tier=action.tier,
        terms=terms,
        terms_hash=terms_hash(terms),
        content=content.to_json(),
        expires_at=expires_at,
        plan_id=visible_plan_id(uow, action),
        logical_action_id=action.logical_action_id,
        revision=action.revision,
        cash_snapshot_id=cash_snapshot_id,
        calc_result_id=calc_result_id,
    )
    audit.record(
        uow,
        AuditActor.CUSTOMER,
        "card_opened",
        customer_id=action.customer_id,
        ref=card.card_id,
        decision=action.tier.value,
    )
    return card


def visible_plan_id(reader: Reader, action: ActionRow) -> str | None:
    """The plan the customer sees for an action: none for a standalone action's own plan."""
    if action.plan_id is None:
        return None
    plan = plans.get_plan(reader, action.plan_id)
    return None if plan.kind is PlanKind.SINGLE else plan.plan_id


def open_plan_card(
    uow: UnitOfWork,
    *,
    customer_id: str,
    plan_id: str,
    terms: Mapping[str, Any],
    content: CardContent,
    expires_at: datetime,
    parent_approval_id: str | None = None,
    cash_snapshot_id: str | None = None,
    calc_result_id: str | None = None,
) -> CardRow:
    """Show a plan (or a revision outside its bounds) on a card; plan approval is YELLOW.

    `terms` are the plan's canonical terms, including its bounds under the key "bounds".
    """
    kind = CardKind.PLAN if parent_approval_id is None else CardKind.PLAN_REVISION
    card = approvals.insert_card(
        uow,
        card_id=new_id(IdPrefix.CARD),
        customer_id=customer_id,
        kind=kind,
        tier=Tier.YELLOW,
        terms=terms,
        terms_hash=terms_hash(terms),
        content=content.to_json(),
        expires_at=expires_at,
        plan_id=plan_id,
        parent_approval_id=parent_approval_id,
        cash_snapshot_id=cash_snapshot_id,
        calc_result_id=calc_result_id,
    )
    audit.record(uow, AuditActor.CUSTOMER, "card_opened", customer_id=customer_id, ref=card.card_id)
    return card


def card_view(reader: Reader, card: CardRow, now: datetime) -> Card:
    action_kind = None
    if card.logical_action_id is not None and card.revision is not None:
        action_kind = actions.get_revision(reader, card.logical_action_id, card.revision).kind
    return Card.model_validate(
        {
            **card.content,
            "card_id": card.card_id,
            "kind": card.kind,
            "status": card_status(reader, card, now),
            "tier": card.tier,
            "requires_code": card.tier is Tier.RED,
            "logical_action_id": card.logical_action_id,
            "revision": card.revision,
            "action_kind": action_kind,
            "plan_id": card.plan_id,
            "parent_approval_id": card.parent_approval_id,
            "approval_id": card.approval_id,
            "cash_snapshot_id": card.cash_snapshot_id,
            "calc_result_id": card.calc_result_id,
            "terms_hash": card.terms_hash,
            "expires_at": card.expires_at,
        }
    )


def find_owned_card(reader: Reader, customer_id: str, card_id: str) -> CardRow:
    return owned(approvals.find_card(reader, card_id), customer_id, lambda c: c.customer_id)


# Approvals (SPEC §9.2)


@dataclass(frozen=True)
class Confirmation:
    card: CardRow
    approval: ApprovalRow
    action: ActionRow | None
    challenge: ChallengeRow | None
    repeated: bool


def confirm_card(
    uow: UnitOfWork,
    step_up: StepUp,
    *,
    customer_id: str,
    card_id: str,
    presented_terms_hash: str,
    now: datetime,
) -> Confirmation:
    """The customer confirms a card with the terms hash it displayed.

    YELLOW and GREEN actions become READY; a RED action moves to AWAITING_STEPUP and a code
    is sent. Confirming a confirmed card again with the same hash changes nothing.
    """
    card = find_owned_card(uow, customer_id, card_id)
    if not _same(presented_terms_hash, card.terms_hash):
        raise WgError(ErrorCode.TERMS_MISMATCH, {"card_id": card.card_id})
    if card.approval_id is not None:
        return _repeat_confirmation(uow, card)
    status = card_status(uow, card, now)
    if status is CardStatus.SUPERSEDED:
        raise WgError(ErrorCode.SUPERSEDED, {"card_id": card.card_id})
    if status is CardStatus.EXPIRED:
        raise WgError(ErrorCode.APPROVAL_EXPIRED, {"card_id": card.card_id})
    if card.kind is not CardKind.ACTION:
        return _confirm_plan_card(uow, card)

    assert card.logical_action_id is not None and card.revision is not None
    action = actions.get_revision(uow, card.logical_action_id, card.revision)
    if not _same(terms_hash(action_terms(action, card.expires_at)), card.terms_hash):
        raise WgError(ErrorCode.TERMS_MISMATCH, {"card_id": card.card_id})
    check_not_locked(uow, customer_id, action.plan_id)
    approval = approvals.insert_approval(
        uow,
        approval_id=new_id(IdPrefix.APPROVAL),
        customer_id=customer_id,
        scope_kind=ApprovalScopeKind.ACTION,
        terms=card.terms,
        terms_hash=card.terms_hash,
        expires_at=card.expires_at,
        plan_id=action.plan_id,
        logical_action_id=action.logical_action_id,
        revision=action.revision,
        cash_snapshot_id=card.cash_snapshot_id,
        calc_result_id=card.calc_result_id,
    )
    approvals.set_card_approval(uow, card.card_id, approval.approval_id)
    audit.record(
        uow,
        AuditActor.CUSTOMER,
        "approval_created",
        customer_id=customer_id,
        ref=approval.approval_id,
        decision=action.tier.value,
    )
    challenge = None
    if action.tier is Tier.RED:
        action = actions.set_status(uow, action, ActionStatus.AWAITING_STEPUP)
        challenge = issue_challenge(uow, step_up, action, approval, now)
    else:
        action = actions.set_status(uow, action, ActionStatus.READY)
    confirmed = approvals.find_card(uow, card.card_id)
    assert confirmed is not None
    return Confirmation(confirmed, approval, action, challenge, repeated=False)


def _confirm_plan_card(uow: UnitOfWork, card: CardRow) -> Confirmation:
    """The plan-scope approval of a confirmed plan card; the coordinator activates its actions."""
    assert card.plan_id is not None
    check_not_locked(uow, card.customer_id, card.plan_id)
    approval = approvals.insert_approval(
        uow,
        approval_id=new_id(IdPrefix.APPROVAL),
        customer_id=card.customer_id,
        scope_kind=ApprovalScopeKind.PLAN,
        terms=card.terms,
        terms_hash=card.terms_hash,
        expires_at=card.expires_at,
        plan_id=card.plan_id,
        bounds=card.terms.get("bounds"),
        parent_approval_id=card.parent_approval_id,
        cash_snapshot_id=card.cash_snapshot_id,
        calc_result_id=card.calc_result_id,
    )
    approvals.set_card_approval(uow, card.card_id, approval.approval_id)
    audit.record(
        uow,
        AuditActor.CUSTOMER,
        "approval_created",
        customer_id=card.customer_id,
        ref=approval.approval_id,
        decision=Tier.YELLOW.value,
    )
    confirmed = approvals.find_card(uow, card.card_id)
    assert confirmed is not None
    return Confirmation(confirmed, approval, None, None, repeated=False)


def _repeat_confirmation(reader: Reader, card: CardRow) -> Confirmation:
    assert card.approval_id is not None
    approval = approvals.find_approval(reader, card.approval_id)
    assert approval is not None
    action = challenge = None
    if card.logical_action_id is not None and card.revision is not None:
        action = actions.get_revision(reader, card.logical_action_id, card.revision)
        issued = challenges.challenges_of_action(reader, action.logical_action_id, action.revision)
        open_ones = [c for c in issued if c.is_open]
        challenge = open_ones[-1] if open_ones else None
    return Confirmation(card, approval, action, challenge, repeated=True)


def approval_for(reader: Reader, action: ActionRow) -> ApprovalRow | None:
    """The approval covering an action: its own (newest) or else its plan's (newest)."""
    own = approvals.approvals_of_action(reader, action.logical_action_id, action.revision)
    if own:
        return own[0]
    if action.plan_id is not None:
        plan = approvals.approvals_of_plan(reader, action.plan_id)
        if plan:
            return plan[0]
    return None


def approval_refusal(approval: ApprovalRow | None, now: datetime) -> ErrorCode | None:
    """Why an approval does not authorize anything at `now`, or None when it does."""
    if approval is None:
        return ErrorCode.APPROVAL_REQUIRED
    if approval.revoked_at is not None:
        return ErrorCode.APPROVAL_REVOKED
    if now >= approval.expires_at:
        return ErrorCode.APPROVAL_EXPIRED
    return None


def valid_approval(reader: Reader, action: ActionRow, now: datetime) -> ApprovalRow:
    """The approval of `action` if it is valid at `now`; the gateway checks this for every tier."""
    approval = approval_for(reader, action)
    code = approval_refusal(approval, now)
    if code is not None:
        raise WgError(code, {"logical_action_id": action.logical_action_id})
    assert approval is not None
    return approval


def revoke_approval(
    uow: UnitOfWork, step_up: StepUp, approval_id: str, now: datetime, actor: AuditActor
) -> list[ChallengeRow]:
    """Revoke an approval and invalidate every open challenge and grant issued under it."""
    approval = approvals.find_approval(uow, approval_id)
    if approval is None:
        raise not_found()
    if not approvals.revoke_approval(uow, approval_id, now):
        return []
    audit.record(uow, actor, "approval_revoked", customer_id=approval.customer_id, ref=approval_id)
    closed = []
    for challenge in challenges.active_challenges(uow, customer_id=approval.customer_id):
        if challenge.approval_id == approval_id:
            closed.append(
                _invalidate(uow, step_up, challenge, now, InvalidationReason.APPROVAL_REVOKED)
            )
    return closed


# Challenges (SPEC §9.3)


def issue_challenge(
    uow: UnitOfWork, step_up: StepUp, action: ActionRow, approval: ApprovalRow, now: datetime
) -> ChallengeRow:
    """Send a new code for one action revision; an open challenge of that revision closes."""
    for previous in challenges.active_challenges(uow, logical_action_id=action.logical_action_id):
        if previous.is_open:
            _invalidate(uow, step_up, previous, now, InvalidationReason.REISSUED)
    challenge_id = new_id(IdPrefix.CHALLENGE)
    scope = scope_hash(action, approval.terms_hash)
    code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
    challenge = challenges.insert_challenge(
        uow,
        challenge_id=challenge_id,
        customer_id=action.customer_id,
        approval_id=approval.approval_id,
        logical_action_id=action.logical_action_id,
        revision=action.revision,
        scope_hash=scope,
        code_hmac=step_up.code_hmac(challenge_id, scope, code),
        max_attempts=MAX_ATTEMPTS,
        issued_at=now,
        expires_at=now + CODE_TTL,
    )
    step_up.provider.deliver(uow, action.customer_id, challenge_id, code)
    audit.record(
        uow,
        AuditActor.CUSTOMER,
        "challenge_issued",
        customer_id=action.customer_id,
        ref=challenge_id,
    )
    return challenge


def request_challenge(
    uow: UnitOfWork, step_up: StepUp, *, customer_id: str, logical_action_id: str, now: datetime
) -> ChallengeRow:
    """The customer asks for a code for an action waiting for step-up ("获取验证码")."""
    action = owned(actions.find_current(uow, logical_action_id), customer_id, _customer)
    approval = approval_for(uow, action)
    code = approval_refusal(approval, now)
    if code is not None:
        raise WgError(code, {"logical_action_id": logical_action_id})
    assert approval is not None
    if action.status is not ActionStatus.AWAITING_STEPUP:
        raise state_refusal(action).error()
    check_not_locked(uow, customer_id, action.plan_id)
    return issue_challenge(uow, step_up, action, approval, now)


def reissue_challenge(
    uow: UnitOfWork, step_up: StepUp, *, customer_id: str, challenge_id: str, now: datetime
) -> ChallengeRow:
    """A new code for the action of an earlier challenge, for example after it expired."""
    previous = owned(challenges.find_challenge(uow, challenge_id), customer_id, _customer)
    current = actions.find_current(uow, previous.logical_action_id)
    assert current is not None
    if current.revision != previous.revision:
        raise WgError(ErrorCode.SUPERSEDED, {"challenge_id": challenge_id})
    return request_challenge(
        uow, step_up, customer_id=customer_id, logical_action_id=current.logical_action_id, now=now
    )


@dataclass(frozen=True)
class Verified:
    challenge: ChallengeRow
    action: ActionRow
    repeated: bool


def verify_code(
    uow: UnitOfWork,
    step_up: StepUp,
    *,
    customer_id: str,
    challenge_id: str,
    code: str,
    now: datetime,
) -> Verified | Refusal:
    """Check a code typed in the trusted UI; a correct one turns the challenge into a grant.

    The code is valid while now < issued_at + 5 minutes and for 3 attempts. A wrong code
    counts an attempt; the third closes the challenge. The action moves to READY.
    """
    challenge = owned(challenges.find_challenge(uow, challenge_id), customer_id, _customer)
    action = actions.get_revision(uow, challenge.logical_action_id, challenge.revision)
    correct = _same(
        step_up.code_hmac(challenge.challenge_id, challenge.scope_hash, code),
        challenge.code_hmac,
    )
    if challenge.consumed_at is not None:
        return _refuse(uow, challenge, ErrorCode.GRANT_CONSUMED)
    if challenge.verified_at is not None and challenge.invalidated_at is None:
        if correct:
            return Verified(challenge, action, repeated=True)
        return _refuse(uow, challenge, ErrorCode.CODE_INVALID)
    if challenge.invalidated_at is not None:
        reason = challenge.invalidation_reason or ""
        return _refuse(uow, challenge, _REFUSAL_AFTER.get(reason, ErrorCode.CODE_INVALID))

    closing = _closing_reason(uow, challenge, action, now)
    if closing is not None:
        reason, refusal = closing
        _invalidate(uow, step_up, challenge, now, reason)
        return _refuse(uow, challenge, refusal)
    if action.status is not ActionStatus.AWAITING_STEPUP:
        return _refuse(uow, challenge, state_refusal(action).code)
    check_not_locked(uow, customer_id, action.plan_id)

    if not correct:
        challenge = challenges.record_failed_attempt(uow, challenge)
        if challenge.attempts >= challenge.max_attempts:
            _invalidate(uow, step_up, challenge, now, InvalidationReason.ATTEMPTS_EXHAUSTED)
            return _refuse(uow, challenge, ErrorCode.CODE_ATTEMPTS_EXHAUSTED)
        return _refuse(
            uow, challenge, ErrorCode.CODE_INVALID, {"attempts_left": challenge.attempts_left}
        )

    challenge = challenges.mark_verified(uow, challenge, now)
    step_up.provider.withdraw(uow, challenge.challenge_id)
    action = actions.set_status(uow, action, ActionStatus.READY)
    audit.record(
        uow,
        AuditActor.CUSTOMER,
        "challenge_verified",
        customer_id=customer_id,
        ref=challenge.challenge_id,
    )
    return Verified(challenge, action, repeated=False)


def _closing_reason(
    reader: Reader, challenge: ChallengeRow, action: ActionRow, now: datetime
) -> tuple[InvalidationReason, ErrorCode] | None:
    """Why an open challenge can no longer be verified, checked before the code itself."""
    if action.status is ActionStatus.SUPERSEDED:
        return InvalidationReason.NEW_REVISION, ErrorCode.SUPERSEDED
    if now >= challenge.expires_at:
        return InvalidationReason.EXPIRED, ErrorCode.CODE_EXPIRED
    approval = approvals.find_approval(reader, challenge.approval_id)
    code = approval_refusal(approval, now)
    if code is ErrorCode.APPROVAL_REVOKED:
        return InvalidationReason.APPROVAL_REVOKED, code
    if code is not None:
        return InvalidationReason.APPROVAL_EXPIRED, ErrorCode.APPROVAL_EXPIRED
    assert approval is not None
    if not _same(scope_hash(action, approval.terms_hash), challenge.scope_hash):
        return InvalidationReason.SCOPE_MISMATCH, ErrorCode.CODE_SCOPE_MISMATCH
    return None


def challenge_view(challenge: ChallengeRow) -> ChallengeView:
    return ChallengeView(
        challenge_id=challenge.challenge_id,
        logical_action_id=challenge.logical_action_id,
        revision=challenge.revision,
        expires_at=challenge.expires_at,
        attempts_left=challenge.attempts_left,
    )


def expire_open_challenges(uow: UnitOfWork, step_up: StepUp, now: datetime) -> int:
    """Close open challenges whose five minutes are over and wipe their codes."""
    expired = challenges.expired_open(uow, now)
    for challenge in expired:
        _invalidate(uow, step_up, challenge, now, InvalidationReason.EXPIRED)
    return len(expired)


# Grants (SPEC §9.3, §12.3)


def find_grant(reader: Reader, action: ActionRow) -> ChallengeRow | None:
    """The unconsumed, valid-looking grant of this action revision, if any."""
    for challenge in challenges.challenges_of_action(
        reader, action.logical_action_id, action.revision
    ):
        if challenge.is_grant:
            return challenge
    return None


def consume_grant(uow: UnitOfWork, action: ActionRow, now: datetime) -> ChallengeRow:
    """Use the grant of a RED action; the gateway calls this inside its submitting unit.

    Raises GRANT_CONSUMED when the only grant was used, APPROVAL_REQUIRED when there is
    none, CODE_EXPIRED when it is older than five minutes, APPROVAL_REVOKED or
    APPROVAL_EXPIRED for its approval, and INTEGRITY_VIOLATION when its scope hash no longer
    matches the action (an authority incident, SPEC §16).
    """
    grant = find_grant(uow, action)
    ref: dict[str, JsonValue] = {"logical_action_id": action.logical_action_id}
    if grant is None:
        used = any(
            c.consumed_at is not None
            for c in challenges.challenges_of_action(uow, action.logical_action_id, action.revision)
        )
        code = ErrorCode.GRANT_CONSUMED if used else ErrorCode.APPROVAL_REQUIRED
        raise WgError(code, ref)
    approval = approvals.find_approval(uow, grant.approval_id)
    code = approval_refusal(approval, now)
    if code is not None:
        raise WgError(code, ref)
    assert approval is not None
    if now >= grant.expires_at:
        raise WgError(ErrorCode.CODE_EXPIRED, ref)
    if not _same(scope_hash(action, approval.terms_hash), grant.scope_hash):
        raise WgError(ErrorCode.INTEGRITY_VIOLATION, {**ref, "reason": "scope_hash"})
    consumed = challenges.mark_consumed(uow, grant, now)
    audit.record(
        uow,
        AuditActor.SYSTEM,
        "grant_consumed",
        customer_id=action.customer_id,
        ref=grant.challenge_id,
    )
    return consumed


def invalidate_grants(
    uow: UnitOfWork,
    step_up: StepUp,
    reason: InvalidationReason,
    now: datetime,
    *,
    customer_id: str | None = None,
    plan_id: str | None = None,
    logical_action_id: str | None = None,
) -> list[ChallengeRow]:
    """Close every open challenge and unconsumed grant in scope (SPEC §9.3, §16).

    For expiry, lock clearance and approval revocation, a READY action whose grant closes
    returns to AWAITING_STEPUP and waits for a new code. For a new revision, a stop or a
    cancel, the caller moves the action itself.
    """
    if customer_id is None and plan_id is None and logical_action_id is None:
        raise ValueError("invalidate_grants needs a scope")
    closed = []
    for challenge in challenges.active_challenges(
        uow, customer_id=customer_id, plan_id=plan_id, logical_action_id=logical_action_id
    ):
        was_grant = challenge.is_grant
        closed.append(_invalidate(uow, step_up, challenge, now, reason))
        if was_grant and reason in _BACK_TO_STEPUP:
            action = actions.get_revision(uow, challenge.logical_action_id, challenge.revision)
            if action.status is ActionStatus.READY:
                actions.set_status(uow, action, ActionStatus.AWAITING_STEPUP)
    return closed


# Helpers


def _customer(record: ActionRow | ChallengeRow) -> str:
    return record.customer_id


def _invalidate(
    uow: UnitOfWork,
    step_up: StepUp,
    challenge: ChallengeRow,
    now: datetime,
    reason: InvalidationReason,
) -> ChallengeRow:
    closed = challenges.invalidate(uow, challenge, now, reason.value)
    step_up.provider.withdraw(uow, challenge.challenge_id)
    audit.record(
        uow,
        AuditActor.SYSTEM,
        "challenge_invalidated",
        customer_id=challenge.customer_id,
        ref=challenge.challenge_id,
        reason=reason.value,
    )
    return closed


def _refuse(
    uow: UnitOfWork,
    challenge: ChallengeRow,
    code: ErrorCode,
    detail: dict[str, JsonValue] | None = None,
) -> Refusal:
    audit.record(
        uow,
        AuditActor.CUSTOMER,
        "verify_refused",
        customer_id=challenge.customer_id,
        ref=challenge.challenge_id,
        decision=code.value,
    )
    return Refusal(code, {"challenge_id": challenge.challenge_id, **(detail or {})})


_STATE_REFUSALS: dict[ActionStatus, ErrorCode] = {
    ActionStatus.DRAFT: ErrorCode.APPROVAL_REQUIRED,
    ActionStatus.AWAITING_APPROVAL: ErrorCode.APPROVAL_REQUIRED,
    ActionStatus.SETTLED: ErrorCode.ALREADY_SETTLED,
    ActionStatus.SUPERSEDED: ErrorCode.SUPERSEDED,
    # A closed action can no longer be approved, as its card shows (status "expired").
    ActionStatus.CANCELLED: ErrorCode.APPROVAL_EXPIRED,
    ActionStatus.REJECTED: ErrorCode.APPROVAL_EXPIRED,
    ActionStatus.PAUSED: ErrorCode.APPROVAL_EXPIRED,
}


def state_refusal(action: ActionRow) -> Refusal:
    """The refusal for a step that the action's current status does not allow.

    READY, SUBMITTED and UNKNOWN actions are past approval and step-up; asking for either
    again is UNSUPPORTED_REQUEST.
    """
    detail: dict[str, JsonValue] = {
        "logical_action_id": action.logical_action_id,
        "status": action.status.value,
    }
    return Refusal(_STATE_REFUSALS.get(action.status, ErrorCode.UNSUPPORTED_REQUEST), detail)
