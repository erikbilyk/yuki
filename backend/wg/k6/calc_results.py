"""Immutable calculation results of the funds engine (SPEC §14.7).

A result stores its basis (holdings snapshots, NAV date, fee terms, taxonomy and, where cash
matters, the cash snapshot) next to its output, so every number on a card or in a reply can
be traced to the inputs it was computed from. A table trigger refuses updates.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from wg.contracts.ids import IdPrefix, new_id
from wg.core.hashes import sha256_hex
from wg.k6.db import Reader, UnitOfWork
from wg.k6.simtime import from_db, now_db


class CalcKind(StrEnum):
    EXPOSURE = "exposure"
    CANDIDATES = "candidates"
    STRESS = "stress"
    PUBLIC_DIAGNOSTICS = "public_diagnostics"


PUBLIC_CUSTOMER_ID = "cus_public"


@dataclass(frozen=True)
class CalcResultRow:
    calc_result_id: str
    customer_id: str
    kind: CalcKind
    inputs_hash: str
    basis: Mapping[str, Any]
    output: Mapping[str, Any]
    created_at: datetime


def _row(row) -> CalcResultRow:
    return CalcResultRow(
        calc_result_id=row["calc_result_id"],
        customer_id=row["customer_id"],
        kind=CalcKind(row["kind"]),
        inputs_hash=row["inputs_hash"],
        basis=json.loads(row["basis_json"]),
        output=json.loads(row["output_json"]),
        created_at=from_db(row["created_at"]),
    )


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def store(
    uow: UnitOfWork,
    *,
    customer_id: str,
    kind: CalcKind,
    inputs: Mapping[str, Any],
    basis: Mapping[str, Any],
    output: Mapping[str, Any],
) -> CalcResultRow:
    """Store a result; the same inputs for the same customer return the stored one."""
    inputs_hash = sha256_hex({"kind": kind.value, "inputs": dict(inputs)})
    row = uow.execute(
        "SELECT * FROM calc_results WHERE customer_id = ? AND kind = ? AND inputs_hash = ?",
        (customer_id, kind.value, inputs_hash),
    ).fetchone()
    if row is not None:
        return _row(row)
    calc_result_id = new_id(IdPrefix.CALC_RESULT)
    uow.execute(
        "INSERT INTO calc_results (calc_result_id, customer_id, kind, inputs_hash, basis_json,"
        " output_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            calc_result_id,
            customer_id,
            kind.value,
            inputs_hash,
            _json(basis),
            _json(output),
            now_db(uow),
        ),
    )
    stored = find(uow, calc_result_id)
    assert stored is not None
    return stored


def find(reader: Reader, calc_result_id: str) -> CalcResultRow | None:
    row = reader.execute(
        "SELECT * FROM calc_results WHERE calc_result_id = ?", (calc_result_id,)
    ).fetchone()
    return None if row is None else _row(row)


def ensure_public_customer(uow: UnitOfWork) -> None:
    """The system row that owns results belonging to no customer; it cannot log in."""
    uow.execute(
        "INSERT OR IGNORE INTO customers (customer_id, handle, password_hash, display_name,"
        " created_at) VALUES (?, 'system-public', '!', 'Public data', ?)",
        (PUBLIC_CUSTOMER_ID, now_db(uow)),
    )


def latest(reader: Reader, kind: CalcKind, customer_id: str) -> CalcResultRow | None:
    row = reader.execute(
        "SELECT * FROM calc_results WHERE customer_id = ? AND kind = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (customer_id, kind.value),
    ).fetchone()
    return None if row is None else _row(row)
