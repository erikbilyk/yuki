"""Failure records and the per-plan breaker counter (SPEC §11, §16).

`record_failure` runs in its own unit of work after the failed unit has rolled back, so
the evidence survives the rollback. Locking the plan at the threshold is K8's decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from wg.contracts.enums import AuditActor
from wg.contracts.errors import ErrorClass
from wg.k6 import audit
from wg.k6.db import Database, Reader, UnitOfWork
from wg.k6.simtime import ClockError, from_db, now_db, wall_now


@dataclass(frozen=True)
class BreakerState:
    plan_id: str
    consecutive_unexpected: int
    last_failure_at: datetime | None
    last_success_at: datetime | None


def _stamp(uow: UnitOfWork) -> str:
    # A failure can happen before any fixture has set the simulated clock.
    try:
        return now_db(uow)
    except ClockError:
        return wall_now()


def _plan_exists(uow: UnitOfWork, plan_id: str) -> bool:
    return uow.execute("SELECT 1 FROM plans WHERE plan_id = ?", (plan_id,)).fetchone() is not None


def record_failure(
    db: Database,
    *,
    error_class: ErrorClass,
    detail: dict[str, str],
    plan_id: str | None = None,
    logical_action_id: str | None = None,
) -> int | None:
    """Write a failure record in a new unit; return the plan's breaker count if it changed.

    Only unexpected failures count toward the breaker, and only for a plan that exists.
    """
    with db.unit_of_work("record_failure") as uow:
        at = _stamp(uow)
        uow.execute(
            "INSERT INTO failure_records (at, plan_id, logical_action_id, error_class, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (at, plan_id, logical_action_id, error_class.value, json.dumps(detail, sort_keys=True)),
        )
        count = None
        if (
            plan_id is not None
            and error_class is ErrorClass.UNEXPECTED
            and _plan_exists(uow, plan_id)
        ):
            uow.execute(
                "INSERT INTO breaker_state"
                " (plan_id, consecutive_unexpected, last_failure_at, created_at)"
                " VALUES (?, 1, ?, ?)"
                " ON CONFLICT (plan_id) DO UPDATE SET"
                " consecutive_unexpected = consecutive_unexpected + 1,"
                " last_failure_at = excluded.last_failure_at, version = version + 1",
                (plan_id, at, at),
            )
            count = breaker_count(uow, plan_id)
        if _clock_set(uow):
            audit.record(
                uow,
                AuditActor.SYSTEM,
                "failure_recorded",
                ref=logical_action_id or plan_id,
                decision=error_class.value,
                reason=detail.get("code") or detail.get("exception"),
            )
        return count


def _clock_set(uow: UnitOfWork) -> bool:
    return uow.execute("SELECT 1 FROM sim_clock WHERE id = 1").fetchone() is not None


def record_success(uow: UnitOfWork, plan_id: str) -> None:
    """Reset the plan's counter after a successful submission or settlement."""
    at = now_db(uow)
    uow.execute(
        "INSERT INTO breaker_state"
        " (plan_id, consecutive_unexpected, last_success_at, created_at) VALUES (?, 0, ?, ?)"
        " ON CONFLICT (plan_id) DO UPDATE SET consecutive_unexpected = 0,"
        " last_success_at = excluded.last_success_at, version = version + 1",
        (plan_id, at, at),
    )


def breaker_count(reader: Reader, plan_id: str) -> int:
    row = reader.execute(
        "SELECT consecutive_unexpected FROM breaker_state WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    return 0 if row is None else int(row[0])


def breaker_state(reader: Reader, plan_id: str) -> BreakerState | None:
    row = reader.execute("SELECT * FROM breaker_state WHERE plan_id = ?", (plan_id,)).fetchone()
    if row is None:
        return None
    return BreakerState(
        plan_id=row["plan_id"],
        consecutive_unexpected=row["consecutive_unexpected"],
        last_failure_at=from_db(row["last_failure_at"]) if row["last_failure_at"] else None,
        last_success_at=from_db(row["last_success_at"]) if row["last_success_at"] else None,
    )


def failure_records(reader: Reader, plan_id: str | None = None) -> list[dict[str, object]]:
    """Failure records, oldest first, optionally for one plan."""
    sql = "SELECT * FROM failure_records"
    params: tuple[str, ...] = ()
    if plan_id is not None:
        sql += " WHERE plan_id = ?"
        params = (plan_id,)
    rows = reader.execute(sql + " ORDER BY failure_id", params).fetchall()
    return [dict(row) | {"detail": json.loads(row["detail"])} for row in rows]
