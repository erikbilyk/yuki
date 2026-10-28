"""Simulator controls for demos and tests: reset, clock, faults, payments in (SPEC §12.7, §15.3)."""

from __future__ import annotations

from datetime import timedelta

from wg.contracts.api import (
    ClockAdvanceRequest,
    ClockView,
    FaultRequest,
    FaultsView,
    FaultView,
    IncomingPaymentRequest,
    SimPostingResult,
    SimResetRequest,
    SimResetResult,
)
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.fixture import FixtureError
from wg.core.k1_session import not_found
from wg.core.k4_approval import StepUp, expire_open_challenges
from wg.k6 import accounts, bank, faults, fixture
from wg.k6.clock import advance_clock
from wg.k6.db import Database, UnitOfWork
from wg.k6.gateway import Gateway
from wg.k6.simtime import ClockError, ClockState, now, read_clock


def clock_view(state: ClockState) -> ClockView:
    return ClockView(now=state.now, epoch=state.epoch, day=state.day)


class SimService:
    def __init__(self, db: Database, step_up: StepUp, gateway: Gateway) -> None:
        self.db = db
        self.step_up = step_up
        self.gateway = gateway

    def reset(self, request: SimResetRequest) -> SimResetResult:
        """Load the profile and variant in place of the current content; the clock returns to T0."""
        try:
            profile = fixture.read_profile(request.profile, request.variant)
        except FixtureError:
            raise WgError(
                ErrorCode.UNSUPPORTED_REQUEST,
                {"profile": request.profile, "variant": request.variant},
            ) from None
        state = fixture.reset(self.db, profile, request.variant)
        return SimResetResult(
            profile=request.profile, variant=request.variant, clock=clock_view(state)
        )

    def advance_clock(self, request: ClockAdvanceRequest) -> ClockView:
        """Move the clock to `to`, or forward by days and minutes; never backwards."""

        def advance(uow: UnitOfWork) -> ClockState:
            current = read_clock(uow)
            if request.to is not None:
                target = request.to
            else:
                target = current.now + timedelta(
                    days=request.days or 0, minutes=request.minutes or 0
                )
            try:
                state = advance_clock(uow, target)
            except ClockError:
                raise WgError(
                    ErrorCode.UNSUPPORTED_REQUEST, {"reason": "clock_backwards"}
                ) from None
            # Codes whose five simulated minutes are over close now, so none stays stored.
            expire_open_challenges(uow, self.step_up, state.now)
            return state

        state = self.db.run_in_uow(advance, "sim clock advance")
        # Settlements that fell due by the new time, each in its own unit of work.
        self.gateway.settle_due()
        return clock_view(state)

    def arm_fault(self, request: FaultRequest) -> FaultsView:
        def arm(uow: UnitOfWork) -> FaultsView:
            faults.arm(uow, request.fault, request.count, request.persistent)
            return FaultsView(
                faults=tuple(
                    FaultView(
                        fault=f.fault,
                        remaining_count=f.remaining_count,
                        persistent=f.persistent,
                        armed_at=f.armed_at,
                    )
                    for f in faults.armed_faults(uow)
                )
            )

        return self.db.run_in_uow(arm, "sim arm fault")

    def incoming_payment(self, request: IncomingPaymentRequest) -> SimPostingResult:
        """Money from outside arrives; payer name and remark are untrusted text (T10)."""

        def post(uow: UnitOfWork) -> SimPostingResult:
            account = accounts.find_account(uow, request.account_id)
            if account is None or account.customer_id is None:
                raise not_found()
            operation = bank.post_incoming_payment(
                uow,
                customer_id=account.customer_id,
                account_id=account.account_id,
                amount_fen=request.amount_fen,
                payer_name=request.payer_name,
                remark=request.remark,
                at=now(uow),
            )
            after = accounts.find_account(uow, account.account_id)
            assert after is not None
            return SimPostingResult(
                operation_id=operation.operation_id,
                account_id=account.account_id,
                amount_fen=request.amount_fen,
                posted_fen_after=after.balance_fen,
            )

        return self.db.run_in_uow(post, "sim incoming payment")
