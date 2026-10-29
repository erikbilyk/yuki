"""Offline extraction and reconciliation of the public fund reports.

Usage: python -m wg.sources.public extract|reconcile

Reads cached PDFs from data/public/raw/ and writes files under data/public/.
It uses no network and no database.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from wg.sources import fees, holdings, overlap, reconcile
from wg.sources.pdftext import read_pages, sha256_file

PUBLIC_DIR = Path(__file__).resolve().parents[3] / "data" / "public"
EXPECTED_OVERLAP = Decimal("26.709382")
# Redemption fee tiers of the C classes as stated in SPEC 14.4: (from_days, to_days, rate).
EXPECTED_TIERS = [(1, 6, Decimal("0.015")), (7, 29, Decimal("0.005")), (30, None, Decimal("0"))]
_PERIOD = re.compile(r"2026\s*年\s*6\s*月\s*30\s*日")


class IdentityError(ValueError):
    """A cached file is not the report the manifest says it is."""


@dataclass(frozen=True)
class FundReport:
    code: str
    parent_name: str
    report_file: str
    report_sha256: str
    summary_file: str
    summary_sha256: str
    rows: list[holdings.Holding]
    totals: holdings.ReportTotals
    tiers: list[fees.FeeTier]


def load_manifest(public_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((public_dir / "reports.yaml").read_text(encoding="utf-8"))


def _verified_pages(raw_dir: Path, entry: dict[str, Any]) -> list[str]:
    path = raw_dir / entry["file"]
    if not path.is_file():
        raise IdentityError(f"missing cached file: {entry['file']}")
    actual = sha256_file(path)
    if actual != entry["sha256"]:
        raise IdentityError(f"SHA-256 mismatch for {entry['file']}: {actual}")
    return read_pages(path)


def load_fund(raw_dir: Path, fund: dict[str, Any]) -> FundReport:
    report = fund["interim_report"]
    summary = fund["product_summary"]
    report_pages = _verified_pages(raw_dir, report)
    summary_pages = _verified_pages(raw_dir, summary)
    cover = report_pages[0]
    if fund["parent_name"] not in cover or not _PERIOD.search(cover):
        raise IdentityError(
            f"{report['file']} is not the 2026-06-30 report of {fund['parent_name']}"
        )
    if fees.parse_share_class_code(summary_pages) != fund["code"]:
        raise IdentityError(f"{summary['file']} does not describe share class {fund['code']}")
    return FundReport(
        code=fund["code"],
        parent_name=fund["parent_name"],
        report_file=report["file"],
        report_sha256=report["sha256"],
        summary_file=summary["file"],
        summary_sha256=summary["sha256"],
        rows=holdings.parse_holdings(report_pages),
        totals=holdings.parse_totals(report_pages),
        tiers=fees.parse_redemption_tiers(summary_pages),
    )


def _write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def write_holdings(public_dir: Path, period_end: str, fund: FundReport) -> Path:
    path = public_dir / f"holdings_{fund.code}.csv"
    header = [
        "fund_code", "period_end", "section", "seq", "security_code", "security_name",
        "quantity", "value_fen", "nav_pct", "market", "source_page", "report_sha256",
    ]  # fmt: skip
    rows = [
        [
            fund.code, period_end, row.section, row.seq, row.security_code, row.security_name,
            row.quantity, row.value_fen, row.nav_pct, "HK" if row.is_hong_kong else "CN",
            row.page, fund.report_sha256,
        ]
        for row in fund.rows
    ]  # fmt: skip
    _write_csv(path, header, rows)
    return path


def _figure(figure: holdings.Figure | None) -> dict[str, int] | None:
    return None if figure is None else {"fen": figure.fen, "page": figure.page}


def write_totals(public_dir: Path, funds: list[FundReport]) -> None:
    payload = {
        fund.code: {
            "report_sha256": fund.report_sha256,
            "stock_investment": _figure(fund.totals.stock_investment),
            "portfolio_total": _figure(fund.totals.portfolio_total),
            "domestic_industry_total": _figure(fund.totals.domestic_industry_total),
            "hong_kong_industry_total": _figure(fund.totals.hong_kong_industry_total),
            "balance_sheet_assets": _figure(fund.totals.balance_sheet_assets),
            "net_assets": _figure(fund.totals.net_assets),
            "class_net_assets_fen": list(fund.totals.class_net_assets),
            "class_net_assets_page": fund.totals.class_net_assets_page,
        }
        for fund in funds
    }
    _write_json(public_dir / "report_totals.json", payload)


def _tier_text(tiers: list[tuple[int, int | None, Decimal]]) -> str:
    return "; ".join(f"{low}-{high or ''}d {rate:.4f}" for low, high, rate in tiers)


def run_checks(funds: list[FundReport]) -> dict[str, list[reconcile.Check]]:
    results = {}
    for fund in funds:
        results[fund.code] = [
            *reconcile.sequence_check(fund.rows),
            *reconcile.amount_checks(fund.rows, fund.totals),
            reconcile.percent_check(fund.rows, fund.totals),
            reconcile.Check(
                "redemption fee tiers = SPEC 14.4",
                _tier_text(EXPECTED_TIERS),
                _tier_text([(t.from_days, t.to_days, t.rate) for t in fund.tiers]),
                fund.tiers[0].page,
            ),
        ]
    return results


def write_fee_schedule(public_dir: Path, funds: list[FundReport]) -> None:
    payload = {
        fund.code: {
            "summary_sha256": fund.summary_sha256,
            "redemption_tiers": [
                {
                    "from_days": tier.from_days,
                    "to_days": tier.to_days,
                    "rate": f"{tier.rate:.4f}",
                    "source_page": tier.page,
                }
                for tier in fund.tiers
            ],
        }
        for fund in funds
    }
    _write_json(public_dir / "fee_schedule.json", payload)


def write_overlap(
    public_dir: Path, period_end: str, fund_a: FundReport, fund_b: FundReport
) -> Decimal:
    rows = overlap.overlap_rows(
        fund_a.rows, fund_a.totals.net_assets.fen, fund_b.rows, fund_b.totals.net_assets.fen
    )
    rows.sort(key=lambda row: (-row.shared_weight, row.security_code))
    percent = overlap.overlap_percent(rows)
    header = [
        "security_code", "security_name", f"value_fen_{fund_a.code}", f"value_fen_{fund_b.code}",
        f"weight_{fund_a.code}", f"weight_{fund_b.code}", "shared_weight",
        f"pages_{fund_a.code}", f"pages_{fund_b.code}",
    ]  # fmt: skip
    _write_csv(
        public_dir / "overlap.csv",
        header,
        [
            [
                row.security_code, row.security_name, row.value_fen_a, row.value_fen_b,
                f"{row.weight_a:.10f}", f"{row.weight_b:.10f}", f"{row.shared_weight:.10f}",
                " ".join(map(str, row.pages_a)), " ".join(map(str, row.pages_b)),
            ]
            for row in rows
        ],
    )  # fmt: skip
    _write_json(
        public_dir / "overlap.json",
        {
            "period_end": period_end,
            "funds": [fund_a.code, fund_b.code],
            "definition": "sum over shared securities of min(value / fund net assets) per fund",
            "net_assets_fen": {
                fund_a.code: fund_a.totals.net_assets.fen,
                fund_b.code: fund_b.totals.net_assets.fen,
            },
            "shared_securities": len(rows),
            "overlap_pct": str(percent),
            "executable": False,
            "report_sha256": {fund_a.code: fund_a.report_sha256, fund_b.code: fund_b.report_sha256},
        },
    )
    return percent


def _load_all(public_dir: Path, raw_dir: Path) -> tuple[dict[str, Any], list[FundReport]]:
    manifest = load_manifest(public_dir)
    return manifest, [load_fund(raw_dir, entry) for entry in manifest["funds"]]


def cmd_extract(public_dir: Path, raw_dir: Path) -> int:
    manifest, funds = _load_all(public_dir, raw_dir)
    for fund in funds:
        path = write_holdings(public_dir, manifest["period_end"], fund)
        print(f"{fund.code}: {len(fund.rows)} rows -> {path.name}")
    write_totals(public_dir, funds)
    write_fee_schedule(public_dir, funds)
    return 0


def cmd_reconcile(public_dir: Path, raw_dir: Path) -> int:
    manifest, funds = _load_all(public_dir, raw_dir)
    for fund in funds:
        write_holdings(public_dir, manifest["period_end"], fund)
    write_totals(public_dir, funds)
    write_fee_schedule(public_dir, funds)
    results = run_checks(funds)
    percent = write_overlap(public_dir, manifest["period_end"], funds[0], funds[1])
    reproduced = percent == EXPECTED_OVERLAP
    _write_json(
        public_dir / "reconciliation.json",
        {
            "period_end": manifest["period_end"],
            "checks": {
                code: [
                    {
                        "name": c.name, "ok": c.ok, "expected": c.expected,
                        "actual": c.actual, "page": c.page,
                    }
                    for c in checks
                ]
                for code, checks in results.items()
            },
            "overlap_pct": str(percent),
            "expected_overlap_pct": str(EXPECTED_OVERLAP),
            "overlap_reproduced": reproduced,
        },
    )  # fmt: skip
    failed = 0
    for code, checks in results.items():
        for check in checks:
            failed += not check.ok
            print(f"{code} {'ok  ' if check.ok else 'FAIL'} {check.name}: {check.actual}")
            if not check.ok:
                print(f"       expected {check.expected}")
    print(
        f"overlap {percent}% (expected {EXPECTED_OVERLAP}%): {'match' if reproduced else 'DIFFERS'}"
    )
    return 0 if failed == 0 and reproduced else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m wg.sources.public")
    parser.add_argument("command", choices=["extract", "reconcile"])
    parser.add_argument("--public-dir", type=Path, default=PUBLIC_DIR)
    args = parser.parse_args(argv)
    raw_dir = args.public_dir / "raw"
    try:
        command = cmd_extract if args.command == "extract" else cmd_reconcile
        return command(args.public_dir, raw_dir)
    except (IdentityError, holdings.ReportFormatError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
