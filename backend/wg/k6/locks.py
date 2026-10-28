"""Locks on a plan, a customer or everything (SPEC §16).

Setting and clearing a lock writes the audit row and the domain event in the same unit.
Whether a write is risk-reducing, and the unlock preconditions, are decided by K8.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from wg.contracts.enums import AuditActor, LockScope
from wg.contracts.events import DomainEventType, LockCleared, LockSet
from wg.contracts.ids import IdPrefix, new_id
from wg.k6 import audit, events
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, now_db

GLOBAL_SCOPE_REF = "global"


@dataclass(frozen=True)
class Lock:
    lock_id: str
    scope: LockScope
    scope_ref: str
    reason: str
    since: datetime
    cleared_at: datetime | None
    cleared_by: str | None
    clear_reason: str | None

    @property
    def active(self) -> bool:
        return self.cleared_at is None


def _lock(row) -> Lock:
    return Lock(
        lock_id=row["lock_id"],
        scope=LockScope(row["scope"]),
        scope_ref=row["scope_ref"],
        reason=row["reason"],
        since=from_db(row["since"]),
        cleared_at=from_db(row["cleared_at"]) if row["cleared_at"] else None,
        cleared_by=row["cleared_by"],
        clear_reason=row["clear_reason"],
    )


def _customer_of(reader: Reader, scope: LockScope, scope_ref: str) -> str | None:
    if scope is LockScope.CUSTOMER:
        return scope_ref
    if scope is LockScope.PLAN:
        row = reader.execute(
            "SELECT customer_id FROM plans WHERE plan_id = ?", (scope_ref,)
        ).fetchone()
        return None if row is None else row["customer_id"]
    return None


def set_lock(uow: UnitOfWork, scope: LockScope, scope_ref: str, reason: str) -> Lock:
    """Lock the scope; if it is already locked, return the active lock unchanged."""
    scope = LockScope(scope)
    if scope is LockScope.GLOBAL:
        scope_ref = GLOBAL_SCOPE_REF
    existing = uow.execute(
        "SELECT * FROM locks WHERE scope = ? AND scope_ref = ? AND cleared_at IS NULL",
        (scope.value, scope_ref),
    ).fetchone()
    if existing is not None:
        return _lock(existing)
    lock_id = new_id(IdPrefix.LOCK)
    at = now_db(uow)
    uow.execute(
        "INSERT INTO locks (lock_id, scope, scope_ref, reason, since, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (lock_id, scope.value, scope_ref, reason, at, at),
    )
    customer_id = _customer_of(uow, scope, scope_ref)
    plan_id = scope_ref if scope is LockScope.PLAN else None
    events.append_event(
        uow,
        DomainEventType.LOCK_SET,
        LockSet(lock_id=lock_id, scope=scope, scope_ref=scope_ref, reason=reason),
        customer_id=customer_id,
        plan_id=plan_id,
    )
    audit.record(
        uow,
        AuditActor.SYSTEM,
        "lock_set",
        customer_id=customer_id,
        ref=lock_id,
        decision=scope.value,
        reason=reason,
    )
    return get_lock(uow, lock_id)


def clear_lock(uow: UnitOfWork, lock_id: str, operator_id: str, clear_reason: str) -> Lock:
    """Clear an active lock in the operator's name. Clearing twice is a ValueError."""
    lock = get_lock(uow, lock_id)
    if not lock.active:
        raise ValueError(f"lock {lock_id} is already cleared")
    uow.execute(
        "UPDATE locks SET cleared_at = ?, cleared_by = ?, clear_reason = ?,"
        " version = version + 1 WHERE lock_id = ? AND cleared_at IS NULL",
        (now_db(uow), operator_id, clear_reason, lock_id),
    )
    customer_id = _customer_of(uow, lock.scope, lock.scope_ref)
    events.append_event(
        uow,
        DomainEventType.LOCK_CLEARED,
        LockCleared(lock_id=lock_id, cleared_by=operator_id, clear_reason=clear_reason),
        customer_id=customer_id,
        plan_id=lock.scope_ref if lock.scope is LockScope.PLAN else None,
    )
    audit.record(
        uow,
        AuditActor.OPERATOR,
        "lock_cleared",
        customer_id=customer_id,
        ref=lock_id,
        decision=operator_id,
        reason=clear_reason,
    )
    return get_lock(uow, lock_id)


def get_lock(reader: Reader, lock_id: str) -> Lock:
    row = reader.execute("SELECT * FROM locks WHERE lock_id = ?", (lock_id,)).fetchone()
    if row is None:
        raise KeyError(lock_id)
    return _lock(row)


def list_locks(reader: Reader, active_only: bool = True) -> list[Lock]:
    sql = "SELECT * FROM locks"
    if active_only:
        sql += " WHERE cleared_at IS NULL"
    return [_lock(row) for row in reader.execute(sql + " ORDER BY since, lock_id").fetchall()]


def locks_in_scope(reader: Reader, customer_id: str, plan_id: str | None = None) -> list[Lock]:
    """Active locks that cover a write by this customer, optionally inside this plan."""
    rows = reader.execute(
        "SELECT * FROM locks WHERE cleared_at IS NULL AND ("
        " scope = 'global'"
        " OR (scope = 'customer' AND scope_ref = ?)"
        " OR (scope = 'plan' AND scope_ref = ?))"
        " ORDER BY since, lock_id",
        (customer_id, plan_id),
    ).fetchall()
    return [_lock(row) for row in rows]
