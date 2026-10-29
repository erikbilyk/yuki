"""Simulator controls for demos and tests: reset, clock, faults, payments in (SPEC §12.7, §15.3)."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
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
from wg.contracts.enums import AuditActor
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.fixture import FixtureError
from wg.core.k1_session import not_found
from wg.core.k5_coordinator import Coordinator
from wg.k6 import accounts, audit, bank, faults, fixture
from wg.k6.db import Database, UnitOfWork
from wg.k6.gateway import CRASH_EXIT_STATUS
from wg.k6.simtime import ClockState, now, read_clock

log = logging.getLogger("wg.sim")


def clock_view(state: ClockState) -> ClockView:
    return ClockView(now=state.now, epoch=state.epoch, day=state.day)


class SimService:
    def __init__(
        self,
        db: Database,
        coordinator: Coordinator,
        *,
        exit_process: Callable[[int], object] = os._exit,
    ) -> None:
        self.db = db
        self.coordinator = coordinator
        self._exit = exit_process

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
        """Walk the clock to `to`, or forward by days and minutes, through every due instant."""
        with self.db.read() as conn:
            current = read_clock(conn).now
        if request.to is not None:
            target = request.to
        else:
            target = current + timedelta(days=request.days or 0, minutes=request.minutes or 0)
        return clock_view(self.coordinator.advance_clock(target))

    def crash(self) -> None:
        """End the process at once, as a power cut would; the next start recovers (SPEC §12.8)."""
        self.db.run_in_uow(
            lambda uow: audit.record(uow, AuditActor.SYSTEM, "sim_crash"), "sim crash"
        )
        log.warning("simulated crash")
        self._exit(CRASH_EXIT_STATUS)

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
