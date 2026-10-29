"""Immutable cash snapshots (SPEC §13.3, S2 §5, §7).

`ensure_snapshot` computes the cash engine's values from the current rows and stores a new
snapshot only when its `inputs_hash` differs from the customer's latest one, so an import
repeated without effect, a read and a re-check all return the same snapshot. The hash covers
every input including the simulated time, so a snapshot is always the picture at its `as_of`.
`commitments_version` is stored with it for the staleness checks of S3.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from wg.contracts.engines import (
    BillState,
    CashInputs,
    CashSnapshotValues,
    SnapshotQuality,
    UnresolvedInput,
)
from wg.contracts.errors import ErrorCode, WgError
from wg.contracts.ids import IdPrefix, new_id
from wg.k6 import commitments
from wg.k6.cash_inputs import cash_inputs_from_db
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, now, now_db, to_db
from wg.numeric import compute_snapshot


@dataclass(frozen=True)
class SnapshotRow:
    snapshot_id: str
    customer_id: str
    as_of: datetime
    rules_version: str
    values: CashSnapshotValues
    unresolved: tuple[UnresolvedInput, ...]
    income_this_month_fen: int
    spent_this_month_fen: int
    commitments_version: int
    created_at: datetime

    @property
    def inputs_hash(self) -> str:
        return self.values.inputs_hash

    @property
    def incomplete(self) -> bool:
        return self.values.quality.incomplete


def _dump(models) -> str:
    return json.dumps([m.model_dump(mode="json") for m in models], ensure_ascii=False)


def _snapshot(row) -> SnapshotRow:
    monthly = json.loads(row["monthly_json"])
    values = CashSnapshotValues(
        c_fen=row["c_fen"],
        e_fen=row["e_fen"],
        l_fen=row["l_fen"],
        b_fen=row["b_fen"],
        m_fen=row["m_fen"],
        u_fen=row["u_fen"],
        protected_fen=row["protected_fen"],
        headroom_fen=row["headroom_fen"],
        shortfall_fen=row["shortfall_fen"],
        bills=tuple(BillState.model_validate(b) for b in json.loads(row["bills_json"])),
        monthly_estimate_fen=monthly["monthly_estimate_fen"],
        quality=SnapshotQuality.model_validate(json.loads(row["quality_json"])),
        inputs_hash=row["inputs_hash"],
    )
    return SnapshotRow(
        snapshot_id=row["snapshot_id"],
        customer_id=row["customer_id"],
        as_of=from_db(row["as_of"]),
        rules_version=row["rules_version"],
        values=values,
        unresolved=tuple(
            UnresolvedInput.model_validate(u) for u in json.loads(row["unresolved_json"])
        ),
        income_this_month_fen=monthly["income_this_month_fen"],
        spent_this_month_fen=monthly["spent_this_month_fen"],
        commitments_version=row["commitments_version"],
        created_at=from_db(row["created_at"]),
    )


def find_snapshot(reader: Reader, snapshot_id: str) -> SnapshotRow | None:
    row = reader.execute(
        "SELECT * FROM cash_snapshots WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchone()
    return None if row is None else _snapshot(row)


def latest_snapshot(reader: Reader, customer_id: str) -> SnapshotRow | None:
    row = reader.execute(
        "SELECT * FROM cash_snapshots WHERE customer_id = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (customer_id,),
    ).fetchone()
    return None if row is None else _snapshot(row)


def current_values(
    reader: Reader, customer_id: str, at: datetime | None = None
) -> tuple[CashInputs, CashSnapshotValues]:
    """The engine's inputs and values from the current rows, stored nowhere."""
    inputs = cash_inputs_from_db(reader, customer_id, at)
    return inputs, compute_snapshot(inputs)


def is_current(reader: Reader, customer_id: str) -> bool:
    """True when the customer's latest snapshot matches the current rows and time."""
    latest = latest_snapshot(reader, customer_id)
    if latest is None:
        return False
    return latest.inputs_hash == current_values(reader, customer_id)[1].inputs_hash


def ensure_snapshot(uow: UnitOfWork, customer_id: str) -> SnapshotRow:
    """The snapshot of the current rows: the latest one if nothing changed, else a new one."""
    inputs, values = current_values(uow, customer_id, now(uow))
    latest = latest_snapshot(uow, customer_id)
    if latest is not None and latest.inputs_hash == values.inputs_hash:
        return latest
    snapshot_id = new_id(IdPrefix.CASH_SNAPSHOT)
    uow.execute(
        "INSERT INTO cash_snapshots (snapshot_id, customer_id, as_of, inputs_hash, rules_version,"
        " c_fen, e_fen, l_fen, b_fen, m_fen, u_fen, protected_fen, headroom_fen, shortfall_fen,"
        " bills_json, unresolved_json, monthly_json, quality_json, commitments_version,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot_id,
            customer_id,
            to_db(inputs.as_of),
            values.inputs_hash,
            inputs.rules_version,
            values.c_fen,
            values.e_fen,
            values.l_fen,
            values.b_fen,
            values.m_fen,
            values.u_fen,
            values.protected_fen,
            values.headroom_fen,
            values.shortfall_fen,
            _dump(values.bills),
            _dump(inputs.unresolved),
            json.dumps(
                {
                    "monthly_estimate_fen": values.monthly_estimate_fen,
                    "income_this_month_fen": inputs.income_this_month_fen,
                    "spent_this_month_fen": inputs.spent_this_month_fen,
                }
            ),
            values.quality.model_dump_json(),
            commitments.commitments_version(uow, customer_id),
            now_db(uow),
        ),
    )
    snapshot = find_snapshot(uow, snapshot_id)
    assert snapshot is not None
    return snapshot


def require_complete(snapshot: SnapshotRow) -> None:
    """An incomplete snapshot stops a plan that would continue on it (SPEC §13.3)."""
    if snapshot.incomplete:
        raise WgError(ErrorCode.DATA_INCOMPLETE, {"snapshot_id": snapshot.snapshot_id})
