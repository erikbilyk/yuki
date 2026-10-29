"""Holdings tables and report totals from an interim fund report."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_AMOUNT = r"-?[\d,]+\.\d{2}"
_ROW = re.compile(
    rf"^(?P<seq>\d+) (?P<code>\d{{5,6}}) (?P<name>\S.*?) (?P<qty>[\d,]+) "
    rf"(?P<value>{_AMOUNT}) (?P<pct>-?\d+\.\d{{2}})$"
)
_SECTION = re.compile(r"^(7\.3(?:\.\d+)?) 期末.*股票投资明细$")


def _two_amounts(label: str) -> str:
    return rf"^{label} (?P<now>{_AMOUNT}) (?P<prev>{_AMOUNT})$"


class ReportFormatError(ValueError):
    """The report text does not have the layout the parser expects."""


@dataclass(frozen=True)
class Holding:
    section: str
    seq: int
    security_code: str
    security_name: str
    quantity: int
    value_fen: int
    nav_pct: Decimal
    page: int

    @property
    def is_hong_kong(self) -> bool:
        return len(self.security_code) == 5


@dataclass(frozen=True)
class Figure:
    """An amount in fen with the page it was read from."""

    fen: int
    page: int


@dataclass(frozen=True)
class ReportTotals:
    stock_investment: Figure
    portfolio_total: Figure
    domestic_industry_total: Figure
    hong_kong_industry_total: Figure | None
    balance_sheet_assets: Figure
    net_assets: Figure
    class_net_assets: tuple[int, ...]
    class_net_assets_page: int


def to_fen(text: str) -> int:
    """Convert a yuan amount with two decimals to fen without rounding."""
    value = Decimal(text.replace(",", "")) * 100
    if value != value.to_integral_value():
        raise ReportFormatError(f"amount has more than two decimals: {text}")
    return int(value)


def iter_lines(pages: list[str]):
    for number, text in enumerate(pages, start=1):
        for line in text.splitlines():
            yield number, line.strip()


def parse_holdings(pages: list[str]) -> list[Holding]:
    """Read every row of the stock holdings tables in section 7.3."""
    rows: list[Holding] = []
    section: str | None = None
    for page, line in iter_lines(pages):
        if "...." in line:
            continue
        header = _SECTION.match(line)
        if header:
            section = header.group(1)
            continue
        if section and line.startswith("7.4 "):
            break
        match = _ROW.match(line)
        if section and match:
            rows.append(
                Holding(
                    section=section,
                    seq=int(match["seq"]),
                    security_code=match["code"],
                    security_name=match["name"],
                    quantity=int(match["qty"].replace(",", "")),
                    value_fen=to_fen(match["value"]),
                    nav_pct=Decimal(match["pct"]),
                    page=page,
                )
            )
    if not rows:
        raise ReportFormatError("no holdings rows found in section 7.3")
    return rows


def _first_figure(pages: list[str], pattern: str, after: str | None = None) -> Figure:
    compiled = re.compile(pattern)
    started = after is None
    for page, line in iter_lines(pages):
        if not started:
            started = line.startswith(after) and "...." not in line
            continue
        match = compiled.match(line)
        if match:
            return Figure(to_fen(match["now"]), page)
    raise ReportFormatError(f"line not found: {pattern}")


def parse_totals(pages: list[str]) -> ReportTotals:
    """Read the totals the holdings must reconcile to."""
    stock = _first_figure(
        pages, rf"^其中：股票 (?P<now>{_AMOUNT}) [\d.]+$", after="7.1 期末基金资产组合情况"
    )
    portfolio = _first_figure(
        pages, rf"^9 合计 (?P<now>{_AMOUNT}) 100\.00$", after="7.1 期末基金资产组合情况"
    )
    assets = _first_figure(pages, _two_amounts("资产总计"))
    net = _first_figure(pages, _two_amounts("净资产合计"))
    domestic, hong_kong = _industry_totals(pages)
    class_values, class_page = _class_net_assets(pages)
    return ReportTotals(
        stock_investment=stock,
        portfolio_total=portfolio,
        domestic_industry_total=domestic,
        hong_kong_industry_total=hong_kong,
        balance_sheet_assets=assets,
        net_assets=net,
        class_net_assets=class_values,
        class_net_assets_page=class_page,
    )


def _industry_totals(pages: list[str]) -> tuple[Figure, Figure | None]:
    """Sum the 合计 lines of 7.2: domestic tables first, Hong Kong table after 7.2.2."""
    total = re.compile(rf"^合计 (?P<now>{_AMOUNT}) -?\d+\.\d{{2}}$")
    in_section = False
    after_hk_header = False
    domestic: list[Figure] = []
    hong_kong: list[Figure] = []
    for page, line in iter_lines(pages):
        if "...." in line:
            continue
        if line.startswith("7.2 报告期末按行业分类"):
            in_section = True
        elif line.startswith("7.3 ") and in_section:
            break
        elif in_section and line.startswith("7.2.2"):
            after_hk_header = True
        match = total.match(line) if in_section else None
        if match:
            (hong_kong if after_hk_header else domestic).append(Figure(to_fen(match["now"]), page))
    if not domestic:
        raise ReportFormatError("no industry total found in section 7.2")
    domestic_sum = Figure(sum(f.fen for f in domestic), domestic[-1].page)
    hk_sum = Figure(sum(f.fen for f in hong_kong), hong_kong[-1].page) if hong_kong else None
    return domestic_sum, hk_sum


def _class_net_assets(pages: list[str]) -> tuple[tuple[int, ...], int]:
    pattern = re.compile(r"^期末基金资产净值 ((?:-?[\d,]+\.\d{2} ?)+)$")
    for page, line in iter_lines(pages):
        match = pattern.match(line)
        if match:
            return tuple(to_fen(token) for token in match.group(1).split()), page
    raise ReportFormatError("class net asset line not found")


def stated_pct(value_fen: int, net_assets_fen: int) -> Decimal:
    """Share of net assets as the report prints it: percent, two decimals, half up."""
    return (Decimal(value_fen) * 100 / Decimal(net_assets_fen)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
