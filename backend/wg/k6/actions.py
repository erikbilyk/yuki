"""Actions: one row per logical action and revision (SPEC §7, §8.1).

Every status change goes through `set_status`, which allows only the changes listed in
ACTION_TRANSITIONS and bumps the row version.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from wg.contracts.enums import ACTION_TRANSITIONS, ActionKind, ActionStatus, Tier
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, now_db, to_db


class IllegalTransition(RuntimeError):
    """A status change that SPEC §8.1 does not allow; callers check the status first."""


@dataclass(frozen=True)
class ActionRow:
    logical_action_id: str
    revision: int
    customer_id: str
    plan_id: str | None
    kind: ActionKind
    status: ActionStatus
    params: Mapping[str, Any]
    request_hash: str
    tier: Tier
    depends_on: tuple[str, ...]
    not_before: datetime | None
    superseded_by: int | None
    pause_reasons: tuple[str, ...]
    provenance: Mapping[str, Any]
    version: int
    created_at: datetime


def _action(row) -> ActionRow:
    return ActionRow(
        logical_action_id=row["logical_action_id"],
        revision=row["revision"],
        customer_id=row["customer_id"],
        plan_id=row["plan_id"],
        kind=ActionKind(row["kind"]),
        status=ActionStatus(row["status"]),
        params=json.loads(row["params_json"]),
        request_hash=row["request_hash"],
        tier=Tier(row["tier"]),
        depends_on=tuple(json.loads(row["depends_on"])),
        not_before=from_db(row["not_before"]) if row["not_before"] else None,
        superseded_by=row["superseded_by"],
        pause_reasons=tuple(json.loads(row["pause_reasons"])),
        provenance=json.loads(row["provenance_json"]),
        version=row["version"],
        created_at=from_db(row["created_at"]),
    )


def params_json(params: Mapping[str, Any]) -> str:
    """Stored form of canonical params: sorted keys, no whitespace."""
    return json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def insert_action(
    uow: UnitOfWork,
    *,
    logical_action_id: str,
    customer_id: str,
    kind: ActionKind,
    params: Mapping[str, Any],
    request_hash: str,
    tier: Tier,
    revision: int = 1,
    plan_id: str | None = None,
    depends_on: Sequence[str] = (),
    not_before: datetime | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> ActionRow:
    """Insert a new revision in DRAFT."""
    uow.execute(
        "INSERT INTO actions (logical_action_id, revision, customer_id, plan_id, kind, status,"
        " params_json, request_hash, tier, depends_on, not_before, provenance_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            logical_action_id,
            revision,
            customer_id,
            plan_id,
            ActionKind(kind).value,
            ActionStatus.DRAFT.value,
            params_json(params),
            request_hash,
            Tier(tier).value,
            json.dumps(list(depends_on)),
            to_db(not_before) if not_before else None,
            params_json(provenance or {}),
            now_db(uow),
        ),
    )
    return get_revision(uow, logical_action_id, revision)


def get_revision(reader: Reader, logical_action_id: str, revision: int) -> ActionRow:
    row = reader.execute(
        "SELECT * FROM actions WHERE logical_action_id = ? AND revision = ?",
        (logical_action_id, revision),
    ).fetchone()
    if row is None:
        raise LookupError(f"no action {logical_action_id} revision {revision}")
    return _action(row)


def find_current(reader: Reader, logical_action_id: str) -> ActionRow | None:
    """The newest revision of a logical action, or None when it does not exist."""
    row = reader.execute(
        "SELECT * FROM actions WHERE logical_action_id = ? ORDER BY revision DESC LIMIT 1",
        (logical_action_id,),
    ).fetchone()
    return None if row is None else _action(row)


def set_tier(uow: UnitOfWork, action: ActionRow, tier: Tier) -> ActionRow:
    """Record the tier recomputed before a card is shown; only a DRAFT row changes tier."""
    if action.status is not ActionStatus.DRAFT:
        raise IllegalTransition(f"{action.logical_action_id} r{action.revision} is {action.status}")
    cursor = uow.execute(
        "UPDATE actions SET tier = ?, version = version + 1"
        " WHERE logical_action_id = ? AND revision = ? AND version = ?",
        (Tier(tier).value, action.logical_action_id, action.revision, action.version),
    )
    if cursor.rowcount != 1:
        raise IllegalTransition(
            f"{action.logical_action_id} r{action.revision} changed since version {action.version}"
        )
    return get_revision(uow, action.logical_action_id, action.revision)


def set_status(
    uow: UnitOfWork,
    action: ActionRow,
    to: ActionStatus,
    *,
    superseded_by: int | None = None,
    pause_reasons: Sequence[str] | None = None,
) -> ActionRow:
    """Move one revision to `to`; the row must still be at `action.version`."""
    allowed = ACTION_TRANSITIONS[action.status]
    if to not in allowed:
        raise IllegalTransition(
            f"{action.logical_action_id} r{action.revision}: {action.status} -> {to}"
        )
    if (to is ActionStatus.SUPERSEDED) != (superseded_by is not None):
        raise IllegalTransition("superseded_by is given exactly when the target is SUPERSEDED")
    if (to is ActionStatus.PAUSED) != bool(pause_reasons):
        raise IllegalTransition("pause reasons are given exactly when the target is PAUSED")
    cursor = uow.execute(
        "UPDATE actions SET status = ?, superseded_by = ?, pause_reasons = ?,"
        " version = version + 1"
        " WHERE logical_action_id = ? AND revision = ? AND version = ?",
        (
            to.value,
            superseded_by,
            json.dumps(list(pause_reasons or ())),
            action.logical_action_id,
            action.revision,
            action.version,
        ),
    )
    if cursor.rowcount != 1:
        raise IllegalTransition(
            f"{action.logical_action_id} r{action.revision} changed since version {action.version}"
        )
    return get_revision(uow, action.logical_action_id, action.revision)


def assign_to_plan(
    uow: UnitOfWork,
    action: ActionRow,
    plan_id: str,
    *,
    depends_on: Sequence[str],
    not_before: datetime | None,
) -> ActionRow:
    """Put a DRAFT into a composed plan with the plan's dependencies and due time."""
    cursor = uow.execute(
        "UPDATE actions SET plan_id = ?, depends_on = ?, not_before = ?, version = version + 1"
        " WHERE logical_action_id = ? AND revision = ? AND version = ? AND status = ?",
        (
            plan_id,
            json.dumps(list(depends_on)),
            to_db(not_before) if not_before else None,
            action.logical_action_id,
            action.revision,
            action.version,
            ActionStatus.DRAFT.value,
        ),
    )
    if cursor.rowcount != 1:
        raise IllegalTransition(
            f"{action.logical_action_id} r{action.revision} changed since version {action.version}"
        )
    return get_revision(uow, action.logical_action_id, action.revision)


def next_open_action(
    reader: Reader, customer_id: str, statuses: Collection[str]
) -> ActionRow | None:
    """The customer's action in one of `statuses` that is due soonest (`not_before`, else age)."""
    marks = ", ".join("?" for _ in statuses)
    row = reader.execute(
        f"SELECT * FROM actions WHERE customer_id = ? AND status IN ({marks})"
        " ORDER BY COALESCE(not_before, created_at), logical_action_id LIMIT 1",
        (customer_id, *sorted(statuses)),
    ).fetchone()
    return None if row is None else _action(row)
