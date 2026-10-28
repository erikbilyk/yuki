"""Moves of the persisted simulated clock (SPEC §15.3): start at the epoch, advance forward."""

from __future__ import annotations

from datetime import datetime

from wg.contracts.enums import AuditActor
from wg.contracts.events import ClockAdvanced, DomainEventType
from wg.k6 import audit, events
from wg.k6.db import UnitOfWork
from wg.k6.simtime import SHANGHAI, ClockError, ClockState, from_db, read_clock, to_db


def start_clock(uow: UnitOfWork, epoch: datetime) -> ClockState:
    """Set the clock to the epoch; only the fixture loader calls this, on an empty clock."""
    stamp = to_db(epoch)
    uow.execute(
        "INSERT INTO sim_clock (id, now, epoch, created_at) VALUES (1, ?, ?, ?)",
        (stamp, stamp, stamp),
    )
    return ClockState(now=from_db(stamp), epoch=from_db(stamp))


def advance_clock(uow: UnitOfWork, target: datetime) -> ClockState:
    """Move the clock forward to `target`, with an audit row and a clock_advanced event.

    Moving to the current time changes nothing; an earlier target raises ClockError.
    """
    state = read_clock(uow)
    if target < state.now:
        raise ClockError(f"clock cannot move backwards from {state.now.isoformat()}")
    if target == state.now:
        return state
    uow.execute(
        "UPDATE sim_clock SET now = ?, version = version + 1 WHERE id = 1", (to_db(target),)
    )
    payload = ClockAdvanced(from_at=state.now, to_at=target)
    events.append_event(uow, DomainEventType.CLOCK_ADVANCED, payload)
    audit.record(
        uow,
        AuditActor.SYSTEM,
        "clock_advanced",
        ref=f"T+{(target.astimezone(SHANGHAI).date() - state.epoch_date).days}",
        reason=f"{state.now.isoformat()} -> {target.astimezone(SHANGHAI).isoformat()}",
    )
    return read_clock(uow)
