"""Armed fault points for demos and tests (SPEC §12.7).

A fault is armed with a count and fires at most that many times; each firing consumes one.
Faults survive a process restart only when armed as persistent: `clear_transient` runs at
startup and removes the others.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from wg.contracts.enums import AuditActor, Fault
from wg.contracts.events import DomainEventType, FaultFired
from wg.k6 import audit, events
from wg.k6.db import Database, Reader, UnitOfWork
from wg.k6.simtime import from_db, now_db


@dataclass(frozen=True)
class ArmedFault:
    fault: Fault
    remaining_count: int
    persistent: bool
    armed_at: datetime


def _armed(row) -> ArmedFault:
    return ArmedFault(
        fault=Fault(row["fault"]),
        remaining_count=row["remaining_count"],
        persistent=bool(row["persistent"]),
        armed_at=from_db(row["armed_at"]),
    )


def arm(uow: UnitOfWork, fault: Fault, count: int = 1, persistent: bool = False) -> ArmedFault:
    """Arm a fault; arming it again replaces its count and persistence."""
    if count < 1:
        raise ValueError("count must be at least 1")
    fault = Fault(fault)
    at = now_db(uow)
    uow.execute(
        "INSERT INTO faults (fault, remaining_count, persistent, armed_at, created_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (fault) DO UPDATE SET remaining_count = excluded.remaining_count,"
        " persistent = excluded.persistent, armed_at = excluded.armed_at",
        (fault.value, count, int(persistent), at, at),
    )
    audit.record(
        uow,
        AuditActor.SYSTEM,
        "fault_armed",
        ref=fault.value,
        decision="persistent" if persistent else "transient",
        reason=f"count={count}",
    )
    return ArmedFault(fault, count, persistent, from_db(at))


def armed_faults(reader: Reader) -> list[ArmedFault]:
    rows = reader.execute("SELECT * FROM faults ORDER BY armed_at, fault").fetchall()
    return [_armed(row) for row in rows]


def consume(uow: UnitOfWork, fault: Fault) -> bool:
    """Fire the fault if it is armed: use up one count and record it. False if not armed.

    The consumption commits with `uow`. A fault that ends the process before that commit
    must be taken with `take` in its own unit first.
    """
    fault = Fault(fault)
    row = uow.execute("SELECT * FROM faults WHERE fault = ?", (fault.value,)).fetchone()
    if row is None:
        return False
    remaining = row["remaining_count"] - 1
    if remaining == 0:
        uow.execute("DELETE FROM faults WHERE fault = ?", (fault.value,))
    else:
        uow.execute(
            "UPDATE faults SET remaining_count = ? WHERE fault = ?", (remaining, fault.value)
        )
    events.append_event(
        uow, DomainEventType.FAULT_FIRED, FaultFired(fault=fault, remaining_count=remaining)
    )
    audit.record(
        uow, AuditActor.SYSTEM, "fault_fired", ref=fault.value, reason=f"remaining={remaining}"
    )
    return True


def take(db: Database, fault: Fault) -> bool:
    """`consume` in a unit of its own, committed before the caller acts on the fault."""
    with db.unit_of_work(f"take fault {Fault(fault).value}") as uow:
        return consume(uow, fault)


def clear_transient(uow: UnitOfWork) -> int:
    """Remove faults not armed as persistent; returns how many were removed."""
    return uow.execute("DELETE FROM faults WHERE persistent = 0").rowcount
