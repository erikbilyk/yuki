"""Checks that tie extracted holdings to the totals printed in the report."""

from __future__ import annotations

from dataclasses import dataclass

from wg.sources.holdings import Holding, ReportTotals, stated_pct


@dataclass(frozen=True)
class Check:
    name: str
    expected: str
    actual: str
    page: int | None = None

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


def _fen(value: int) -> str:
    return f"{value} fen"


def sequence_check(rows: list[Holding]) -> list[Check]:
    """Row numbers of each table must run 1..n without gaps, so no row was dropped."""
    checks = []
    for section in sorted({row.section for row in rows}):
        seqs = [row.seq for row in rows if row.section == section]
        contiguous = seqs == list(range(1, len(seqs) + 1))
        checks.append(
            Check(
                f"row numbers {section} run 1..{len(seqs)}",
                "contiguous",
                "contiguous" if contiguous else f"gap or repeat near {seqs[:5]}",
            )
        )
    return checks


def amount_checks(rows: list[Holding], totals: ReportTotals) -> list[Check]:
    domestic = sum(row.value_fen for row in rows if not row.is_hong_kong)
    hong_kong = sum(row.value_fen for row in rows if row.is_hong_kong)
    checks = [
        Check(
            "sum of all holdings = 7.1 stock investment",
            _fen(totals.stock_investment.fen),
            _fen(domestic + hong_kong),
            totals.stock_investment.page,
        ),
        Check(
            "sum of 6-digit holdings = 7.2.1 industry totals",
            _fen(totals.domestic_industry_total.fen),
            _fen(domestic),
            totals.domestic_industry_total.page,
        ),
    ]
    hk_total = totals.hong_kong_industry_total
    checks.append(
        Check(
            "sum of 5-digit holdings = 7.2.2 Hong Kong total",
            _fen(hk_total.fen if hk_total else 0),
            _fen(hong_kong),
            hk_total.page if hk_total else None,
        )
    )
    checks.append(
        Check(
            "7.1 portfolio total = balance sheet total assets",
            _fen(totals.balance_sheet_assets.fen),
            _fen(totals.portfolio_total.fen),
            totals.balance_sheet_assets.page,
        )
    )
    checks.append(
        Check(
            "sum of share class net assets = balance sheet net assets",
            _fen(totals.net_assets.fen),
            _fen(sum(totals.class_net_assets)),
            totals.class_net_assets_page,
        )
    )
    return checks


def percent_check(rows: list[Holding], totals: ReportTotals) -> Check:
    """Rows whose printed share of net assets differs from value / net assets."""
    off = [row for row in rows if stated_pct(row.value_fen, totals.net_assets.fen) != row.nav_pct]
    return Check(
        "printed share of net assets = value / net assets (two decimals)",
        "0 rows differ",
        f"{len(off)} rows differ",
    )
