"""Audit rows for refused requests, written in a unit of their own (SPEC §16)."""

from __future__ import annotations

from wg.contracts.enums import AuditActor
from wg.contracts.errors import WgError
from wg.k6 import audit
from wg.k6.db import Database


def record_refusal(
    db: Database,
    customer_id: str,
    event: str,
    ref: str,
    error: WgError,
    *,
    actor: AuditActor = AuditActor.CUSTOMER,
) -> None:
    """One audit row for a refused customer request, in a unit of its own."""
    db.run_in_uow(
        lambda uow: audit.record(
            uow,
            actor,
            event,
            customer_id=customer_id,
            ref=ref[:64],
            decision=error.code.value,
        ),
        "audit refusal",
    )
