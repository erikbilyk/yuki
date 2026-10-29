"""Import of the dated public fund diagnostics (SPEC §22).

Reads the files the offline parser wrote under `data/public/`, checks them against each
other and against the report hashes in `reports.yaml`, and stores one `public_diagnostics`
calc result owned by the system customer. Nothing here parses a PDF or reaches the network.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml

from wg.contracts.api import PublicDiagnosticsView
from wg.contracts.enums import AuditActor
from wg.contracts.errors import ErrorCode, WgError
from wg.k6 import audit, calc_results
from wg.k6.calc_results import PUBLIC_CUSTOMER_ID, CalcKind, CalcResultRow
from wg.k6.db import Database, UnitOfWork, open_database
from wg.k6.migrate import migrate
from wg.settings import REPO_ROOT, load_settings

PUBLIC_DIR = REPO_ROOT / "data" / "public"
OVERLAP_PLACES = Decimal("0.000001")


class PublicImportError(Exception):
    """The files are missing, disagree with each other or fail a reconciliation check."""


def _read_json(public_dir: Path, name: str) -> Any:
    try:
        return json.loads((public_dir / name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PublicImportError(f"cannot read {name}: {error}") from error


def _holdings_by_code(public_dir: Path, fund_code: str, report_sha256: str) -> dict[str, int]:
    """Value in fen per security code; a code held in two sleeves is summed."""
    values: dict[str, int] = defaultdict(int)
    path = public_dir / f"holdings_{fund_code}.csv"
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row["fund_code"] != fund_code or row["report_sha256"] != report_sha256:
                    raise PublicImportError(f"{path.name}: a row belongs to another report")
                values[row["security_code"]] += int(row["value_fen"])
    except (OSError, KeyError, ValueError) as error:
        raise PublicImportError(f"cannot read {path.name}: {error}") from error
    return values


def recompute_overlap(
    left: Mapping[str, int], right: Mapping[str, int], left_net: int, right_net: int
) -> tuple[int, Decimal]:
    """Shared security count and Σ min(value / net assets) in percent, rounded to 6 places."""
    shared = left.keys() & right.keys()
    total = sum(
        (min(Decimal(left[code]) / left_net, Decimal(right[code]) / right_net) for code in shared),
        Decimal(0),
    )
    return len(shared), (total * 100).quantize(OVERLAP_PLACES, rounding=ROUND_HALF_UP)


def build_output(public_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The stored `(inputs, output)` of the diagnostics, after every consistency check."""
    reports = yaml.safe_load((public_dir / "reports.yaml").read_text(encoding="utf-8"))
    totals = _read_json(public_dir, "report_totals.json")
    overlap = _read_json(public_dir, "overlap.json")
    fees = _read_json(public_dir, "fee_schedule.json")
    reconciliation = _read_json(public_dir, "reconciliation.json")

    failed = [
        f"{code}: {check['name']}"
        for code, checks in reconciliation["checks"].items()
        for check in checks
        if not check["ok"]
    ]
    if failed or not reconciliation["overlap_reproduced"]:
        raise PublicImportError(f"reconciliation did not pass: {failed or 'overlap'}")

    period_end = str(reports["period_end"])
    if not (period_end == overlap["period_end"] == reconciliation["period_end"]):
        raise PublicImportError("report dates differ between the files")

    funds: list[dict[str, Any]] = []
    values: dict[str, dict[str, int]] = {}
    for fund in reports["funds"]:
        code = fund["code"]
        report = fund["interim_report"]
        for name, recorded in (
            ("report_totals", totals[code]["report_sha256"]),
            ("overlap", overlap["report_sha256"][code]),
        ):
            if recorded != report["sha256"]:
                raise PublicImportError(f"{code}: {name} was made from another report")
        if fees[code]["summary_sha256"] != fund["product_summary"]["sha256"]:
            raise PublicImportError(f"{code}: fee schedule was made from another summary")
        net_assets = int(totals[code]["net_assets"]["fen"])
        if int(overlap["net_assets_fen"][code]) != net_assets:
            raise PublicImportError(f"{code}: net assets differ between the files")
        values[code] = _holdings_by_code(public_dir, code, report["sha256"])
        funds.append(
            {
                "product_id": code,
                "name": fund["name"],
                "share_class": fund["share_class"],
                "net_assets_fen": net_assets,
                "redemption_tiers": [
                    {"from_days": t["from_days"], "to_days": t["to_days"], "rate": t["rate"]}
                    for t in fees[code]["redemption_tiers"]
                ],
                "report_title": report["title"],
                "report_sha256": report["sha256"],
                "source_url": report["source_url"],
            }
        )

    first, second = (fund["product_id"] for fund in funds)
    shared, percent = recompute_overlap(
        values[first],
        values[second],
        funds[0]["net_assets_fen"],
        funds[1]["net_assets_fen"],
    )
    if percent != Decimal(overlap["overlap_pct"]) or shared != overlap["shared_securities"]:
        raise PublicImportError(f"overlap recomputed as {percent} % over {shared} securities")

    output = {
        "period_end": period_end,
        "retrieved_on": str(reports["retrieved_on"]),
        "executable": False,
        "funds": funds,
        "overlap": {
            "definition": overlap["definition"],
            "shared_securities": shared,
            "overlap_percent": format(percent, "f"),
        },
    }
    inputs = {
        "period_end": period_end,
        "reports": {fund["product_id"]: fund["report_sha256"] for fund in funds},
        "overlap_percent": output["overlap"]["overlap_percent"],
    }
    return inputs, output


def import_public(uow: UnitOfWork, public_dir: Path = PUBLIC_DIR) -> CalcResultRow:
    """Store the diagnostics as a calc result; the same reports return the stored row."""
    inputs, output = build_output(public_dir)
    calc_results.ensure_public_customer(uow)
    stored = calc_results.store(
        uow,
        customer_id=PUBLIC_CUSTOMER_ID,
        kind=CalcKind.PUBLIC_DIAGNOSTICS,
        inputs=inputs,
        basis={"period_end": output["period_end"], "executable": False},
        output=output,
    )
    audit.record(
        uow,
        AuditActor.SYSTEM,
        "public_diagnostics_imported",
        ref=stored.calc_result_id,
        decision="stored",
    )
    return stored


def public_diagnostics(db: Database) -> PublicDiagnosticsView:
    """The newest imported diagnostics; `DATA_INCOMPLETE` while none is loaded."""
    with db.read() as conn:
        row = calc_results.latest(conn, CalcKind.PUBLIC_DIAGNOSTICS, PUBLIC_CUSTOMER_ID)
    if row is None:
        raise WgError(ErrorCode.DATA_INCOMPLETE, {"reason": "no_public_diagnostics"})
    return PublicDiagnosticsView(calc_result_id=row.calc_result_id, **row.output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m wg.services.public_import")
    parser.add_argument("--db", type=Path, help="database file (default: WG_DB_PATH)")
    parser.add_argument("--public-dir", type=Path, default=PUBLIC_DIR)
    args = parser.parse_args(argv)
    db = open_database(args.db or load_settings().db_path)
    migrate(db)
    try:
        stored = db.run_in_uow(lambda uow: import_public(uow, args.public_dir), "public import")
    except PublicImportError as error:
        print(f"public import refused: {error}", file=sys.stderr)
        return 1
    print(f"stored {stored.calc_result_id} period_end={stored.output['period_end']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
