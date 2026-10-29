"""Domain events and consumer cursors (SPEC §11).

Events are written in the same unit of work as the change they describe. Consumers read
the events after their cursor and move the cursor in the unit that handles them, so
processing resumes from the database after a restart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from wg.contracts._base import ContractModel
from wg.contracts.events import EVENT_PAYLOADS, DomainEventType
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, now_db

_NO_EVENT = "evt_000000000000"


@dataclass(frozen=True)
class DomainEvent:
    event_id: str
    type: DomainEventType
    customer_id: str | None
    plan_id: str | None
    payload: ContractModel
    created_at: datetime


def event_id_for(seq: int) -> str:
    """Zero-padded, so ordering by event_id equals insertion order."""
    return f"evt_{seq:012d}"


def append_event(
    uow: UnitOfWork,
    event_type: DomainEventType,
    payload: ContractModel,
    *,
    customer_id: str | None = None,
    plan_id: str | None = None,
) -> str:
    """Append one event; the payload must be the model registered for its type."""
    expected = EVENT_PAYLOADS[event_type]
    if type(payload) is not expected:
        raise TypeError(f"{event_type} needs a {expected.__name__} payload")
    seq = uow.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM domain_events").fetchone()[0]
    event_id = event_id_for(seq)
    uow.execute(
        "INSERT INTO domain_events"
        " (seq, event_id, type, customer_id, plan_id, payload_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            seq,
            event_id,
            event_type.value,
            customer_id,
            plan_id,
            payload.model_dump_json(),
            now_db(uow),
        ),
    )
    return event_id


def latest_event_id(reader: Reader, event_type: DomainEventType, customer_id: str) -> str | None:
    """The newest event of a type for a customer, or None."""
    row = reader.execute(
        "SELECT MAX(event_id) FROM domain_events WHERE type = ? AND customer_id = ?",
        (event_type.value, customer_id),
    ).fetchone()
    return None if row is None else row[0]


def _event(row) -> DomainEvent:
    event_type = DomainEventType(row["type"])
    payload = EVENT_PAYLOADS[event_type].model_validate(json.loads(row["payload_json"]))
    return DomainEvent(
        event_id=row["event_id"],
        type=event_type,
        customer_id=row["customer_id"],
        plan_id=row["plan_id"],
        payload=payload,
        created_at=from_db(row["created_at"]),
    )


def cursor_position(reader: Reader, consumer: str) -> str:
    """The last event the consumer has processed, or a value before every event."""
    row = reader.execute(
        "SELECT last_event_id FROM event_cursors WHERE consumer = ?", (consumer,)
    ).fetchone()
    return _NO_EVENT if row is None else row["last_event_id"]


def unprocessed_events(
    reader: Reader, consumer: str, limit: int | None = None
) -> list[DomainEvent]:
    """Events after the consumer's cursor, oldest first."""
    rows = reader.execute(
        "SELECT * FROM domain_events WHERE event_id > ? ORDER BY event_id LIMIT ?",
        (cursor_position(reader, consumer), -1 if limit is None else limit),
    ).fetchall()
    return [_event(row) for row in rows]


def mark_processed(uow: UnitOfWork, consumer: str, event_id: str) -> None:
    """Move the consumer's cursor to `event_id`; a cursor never moves backwards."""
    known = uow.execute("SELECT 1 FROM domain_events WHERE event_id = ?", (event_id,))
    if known.fetchone() is None:
        raise ValueError(f"unknown event {event_id}")
    uow.execute(
        "INSERT INTO event_cursors (consumer, last_event_id, created_at) VALUES (?, ?, ?)"
        " ON CONFLICT (consumer) DO UPDATE SET"
        " last_event_id = excluded.last_event_id, version = version + 1"
        " WHERE excluded.last_event_id > event_cursors.last_event_id",
        (consumer, event_id, now_db(uow)),
    )
