"""Simulator controls for demos and tests: reset, clock, faults (SPEC §12.7, §15.3, §19.2)."""

from __future__ import annotations

from datetime import timedelta

from wg.contracts.api import (
    ClockAdvanceRequest,
    ClockView,
    FaultRequest,
    FaultsView,
    FaultView,
    SimResetRequest,
    SimResetResult,
)
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.fixture import FixtureError
from wg.k6 import faults, fixture
from wg.k6.clock import advance_clock
from wg.k6.db import Database, UnitOfWork
from wg.k6.simtime import ClockError, ClockState, read_clock


def clock_view(state: ClockState) -> ClockView:
    return ClockView(now=state.now, epoch=state.epoch, day=state.day)


class SimService:
    def __init__(self, db: Database) -> None:
        self.db = db

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
                return advance_clock(uow, target)
            except ClockError:
                raise WgError(
                    ErrorCode.UNSUPPORTED_REQUEST, {"reason": "clock_backwards"}
                ) from None

        return clock_view(self.db.run_in_uow(advance, "sim clock advance"))

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
