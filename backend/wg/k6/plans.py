"""Plans and their nodes (SPEC §7, §8.2, §15).

A plan of kind `single` holds one standalone action, so the coordinator and the breaker
treat every action the same way. Plan status changes only through `set_status`; `completed`
and `partial` are final.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from wg.contracts.enums import PlanKind, PlanMode, PlanStatus
from wg.contracts.ids import IdPrefix, new_id
from wg.k6 import actions
from wg.k6.actions import ActionRow, IllegalTransition
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, now_db, to_db

FINAL_STATUSES = frozenset({PlanStatus.COMPLETED, PlanStatus.PARTIAL})


@dataclass(frozen=True)
class PlanRow:
    plan_id: str
    customer_id: str
    kind: PlanKind
    status: PlanStatus
    root_approval_id: str | None
    active_revision: int
    mode: PlanMode
    version: int
    created_at: datetime

    @property
    def final(self) -> bool:
        return self.status in FINAL_STATUSES


def _plan(row) -> PlanRow:
    return PlanRow(
        plan_id=row["plan_id"],
        customer_id=row["customer_id"],
        kind=PlanKind(row["kind"]),
        status=PlanStatus(row["status"]),
        root_approval_id=row["root_approval_id"],
        active_revision=row["active_revision"],
        mode=PlanMode(row["mode"]),
        version=row["version"],
        created_at=from_db(row["created_at"]),
    )


def insert_plan(
    uow: UnitOfWork,
    customer_id: str,
    kind: PlanKind,
    mode: PlanMode = PlanMode.COORDINATED,
) -> PlanRow:
    """A new plan in `draft`."""
    plan_id = new_id(IdPrefix.PLAN)
    uow.execute(
        "INSERT INTO plans (plan_id, customer_id, kind, status, mode, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            plan_id,
            customer_id,
            PlanKind(kind).value,
            PlanStatus.DRAFT.value,
            PlanMode(mode).value,
            now_db(uow),
        ),
    )
    return get_plan(uow, plan_id)


def find_plan(reader: Reader, plan_id: str) -> PlanRow | None:
    row = reader.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
    return None if row is None else _plan(row)


def get_plan(reader: Reader, plan_id: str) -> PlanRow:
    plan = find_plan(reader, plan_id)
    if plan is None:
        raise LookupError(f"no plan {plan_id}")
    return plan


def set_status(uow: UnitOfWork, plan: PlanRow, to: PlanStatus) -> PlanRow:
    """Move the plan to `to`; a final plan never changes and the row must be at its version."""
    if plan.final and to is not plan.status:
        raise IllegalTransition(f"plan {plan.plan_id} is {plan.status}")
    if to is plan.status:
        return plan
    cursor = uow.execute(
        "UPDATE plans SET status = ?, version = version + 1 WHERE plan_id = ? AND version = ?",
        (PlanStatus(to).value, plan.plan_id, plan.version),
    )
    if cursor.rowcount != 1:
        raise IllegalTransition(f"plan {plan.plan_id} changed since version {plan.version}")
    return get_plan(uow, plan.plan_id)


def set_root_approval(uow: UnitOfWork, plan: PlanRow, approval_id: str) -> PlanRow:
    """Record the plan-scope approval that activated the plan; it is set once."""
    cursor = uow.execute(
        "UPDATE plans SET root_approval_id = ?, version = version + 1"
        " WHERE plan_id = ? AND version = ? AND root_approval_id IS NULL",
        (approval_id, plan.plan_id, plan.version),
    )
    if cursor.rowcount != 1:
        raise IllegalTransition(f"plan {plan.plan_id} already has a root approval")
    return get_plan(uow, plan.plan_id)


def attach_single(uow: UnitOfWork, action: ActionRow) -> ActionRow:
    """Give a standalone DRAFT action its own plan of kind `single`."""
    if action.plan_id is not None:
        raise IllegalTransition(f"{action.logical_action_id} already belongs to a plan")
    plan = insert_plan(uow, action.customer_id, PlanKind.SINGLE)
    cursor = uow.execute(
        "UPDATE actions SET plan_id = ?, version = version + 1"
        " WHERE logical_action_id = ? AND revision = ? AND version = ? AND plan_id IS NULL",
        (plan.plan_id, action.logical_action_id, action.revision, action.version),
    )
    if cursor.rowcount != 1:
        raise IllegalTransition(
            f"{action.logical_action_id} r{action.revision} changed since version {action.version}"
        )
    return actions.get_revision(uow, action.logical_action_id, action.revision)


def nodes(reader: Reader, plan_id: str) -> list[ActionRow]:
    """The newest revision of every logical action in the plan, in insertion order."""
    rows = reader.execute(
        "SELECT a.logical_action_id, MAX(a.revision) AS revision FROM actions a"
        " WHERE a.plan_id = ? GROUP BY a.logical_action_id ORDER BY MIN(a.rowid)",
        (plan_id,),
    ).fetchall()
    return [actions.get_revision(reader, row["logical_action_id"], row["revision"]) for row in rows]


def plans_to_tick(reader: Reader, customer_id: str | None = None) -> list[str]:
    """Plans a tick visits: those not final, and final ones with an operation still open."""
    sql = (
        "SELECT p.plan_id FROM plans p WHERE (p.status NOT IN ('completed', 'partial')"
        " OR EXISTS (SELECT 1 FROM actions a JOIN operations o"
        " ON o.logical_action_id = a.logical_action_id AND o.revision = a.revision"
        " WHERE a.plan_id = p.plan_id AND o.status IN ('SUBMITTED', 'UNKNOWN')))"
    )
    params: tuple[str, ...] = ()
    if customer_id is not None:
        sql += " AND p.customer_id = ?"
        params = (customer_id,)
    rows = reader.execute(sql + " ORDER BY p.created_at, p.plan_id", params).fetchall()
    return [row["plan_id"] for row in rows]


def checkpoint_result(reader: Reader, plan_id: str, logical_action_id: str) -> str | None:
    """The recorded result of a CHECK_OUTCOME node, or None before it is evaluated."""
    row = reader.execute(
        "SELECT result FROM checkpoint_results WHERE plan_id = ? AND logical_action_id = ?",
        (plan_id, logical_action_id),
    ).fetchone()
    return None if row is None else row["result"]


def next_due_instant(reader: Reader, after: datetime, before: datetime) -> datetime | None:
    """The earliest instant strictly between `after` and `before` at which something is due.

    Due instants (SPEC §15.3): settlement times of open operations, `not_before` of DRAFT
    nodes and the expiry of the root approval of plans that are not final.
    """
    bounds = (to_db(after), to_db(before))
    row = reader.execute(
        "SELECT MIN(t) FROM ("
        " SELECT settle_due_at AS t FROM operations"
        "  WHERE status IN ('SUBMITTED', 'UNKNOWN') AND settle_due_at > ? AND settle_due_at < ?"
        " UNION ALL"
        " SELECT a.not_before FROM actions a JOIN plans p ON p.plan_id = a.plan_id"
        "  WHERE a.status = 'DRAFT' AND p.status NOT IN ('completed', 'partial')"
        "  AND a.not_before > ? AND a.not_before < ?"
        " UNION ALL"
        " SELECT ap.expires_at FROM plans p"
        "  JOIN approvals ap ON ap.approval_id = p.root_approval_id"
        "  WHERE p.status NOT IN ('completed', 'partial')"
        "  AND ap.expires_at > ? AND ap.expires_at < ?)",
        bounds * 3,
    ).fetchone()
    return None if row is None or row[0] is None else from_db(row[0])


# Revisions and checkpoint results (SPEC §7; both append-only)


@dataclass(frozen=True)
class RevisionRow:
    plan_id: str
    revision: int
    reason: str
    trigger_event_id: str | None
    cash_snapshot_id: str | None
    calc_result_id: str | None
    approval_id: str | None
    within_bounds: bool
    created_at: datetime


def insert_revision(
    uow: UnitOfWork,
    *,
    plan_id: str,
    revision: int,
    reason: str,
    cash_snapshot_id: str | None,
    calc_result_id: str | None,
    approval_id: str | None,
    within_bounds: bool,
    trigger_event_id: str | None = None,
) -> None:
    uow.execute(
        "INSERT INTO plan_revisions (plan_id, revision, reason, trigger_event_id,"
        " cash_snapshot_id, calc_result_id, approval_id, within_bounds, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            plan_id,
            revision,
            reason,
            trigger_event_id,
            cash_snapshot_id,
            calc_result_id,
            approval_id,
            int(within_bounds),
            now_db(uow),
        ),
    )


def revisions(reader: Reader, plan_id: str) -> list[RevisionRow]:
    rows = reader.execute(
        "SELECT * FROM plan_revisions WHERE plan_id = ? ORDER BY revision", (plan_id,)
    ).fetchall()
    return [
        RevisionRow(
            plan_id=row["plan_id"],
            revision=row["revision"],
            reason=row["reason"],
            trigger_event_id=row["trigger_event_id"],
            cash_snapshot_id=row["cash_snapshot_id"],
            calc_result_id=row["calc_result_id"],
            approval_id=row["approval_id"],
            within_bounds=bool(row["within_bounds"]),
            created_at=from_db(row["created_at"]),
        )
        for row in rows
    ]


@dataclass(frozen=True)
class CheckpointRow:
    plan_id: str
    logical_action_id: str
    result: str
    measured_pct: str
    lower_pct: str
    upper_pct: str
    basis: dict
    evaluated_at: datetime


def insert_checkpoint(
    uow: UnitOfWork,
    *,
    plan_id: str,
    logical_action_id: str,
    result: str,
    measured_pct: str,
    lower_pct: str,
    upper_pct: str,
    basis: dict,
    evaluated_at: datetime,
) -> bool:
    """Write the node's one result; False when it was already written (never rewritten)."""
    cursor = uow.execute(
        "INSERT INTO checkpoint_results (plan_id, logical_action_id, result, measured_pct,"
        " lower_pct, upper_pct, basis_json, evaluated_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (plan_id, logical_action_id) DO NOTHING",
        (
            plan_id,
            logical_action_id,
            result,
            measured_pct,
            lower_pct,
            upper_pct,
            json.dumps(basis, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            to_db(evaluated_at),
            now_db(uow),
        ),
    )
    return cursor.rowcount == 1


def find_checkpoint(reader: Reader, plan_id: str, logical_action_id: str) -> CheckpointRow | None:
    row = reader.execute(
        "SELECT * FROM checkpoint_results WHERE plan_id = ? AND logical_action_id = ?",
        (plan_id, logical_action_id),
    ).fetchone()
    if row is None:
        return None
    return CheckpointRow(
        plan_id=row["plan_id"],
        logical_action_id=row["logical_action_id"],
        result=row["result"],
        measured_pct=row["measured_pct"],
        lower_pct=row["lower_pct"],
        upper_pct=row["upper_pct"],
        basis=json.loads(row["basis_json"]),
        evaluated_at=from_db(row["evaluated_at"]),
    )
