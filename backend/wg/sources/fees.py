"""Redemption fee schedule and fund identity from a product summary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from wg.sources.holdings import ReportFormatError, iter_lines


@dataclass(frozen=True)
class FeeTier:
    from_days: int
    to_days: int | None
    rate: Decimal
    page: int


_BOUNDED = re.compile(r"(\d+)天\s*(<|≤)\s*N\s*≤\s*(\d+)天\s*([\d.]+)%")
_OPEN = re.compile(r"N\s*≥\s*(\d+)天\s*([\d.]+)%")
_SUB_CODE = re.compile(r"下属基金代码\s*(\d{6})")


def parse_redemption_tiers(pages: list[str]) -> list[FeeTier]:
    """Read the holding-period tiers; days are whole days, so 0 < N becomes 1 <= N."""
    tiers: list[FeeTier] = []
    for page, line in iter_lines(pages):
        bounded = _BOUNDED.search(line)
        if bounded:
            low, op, high, rate = bounded.groups()
            start = int(low) + 1 if op == "<" else int(low)
            tiers.append(FeeTier(start, int(high), Decimal(rate) / 100, page))
            continue
        tail = _OPEN.search(line)
        if tail:
            tiers.append(FeeTier(int(tail[1]), None, Decimal(tail[2]) / 100, page))
    if len(tiers) != 3:
        raise ReportFormatError(f"expected 3 redemption fee tiers, found {len(tiers)}")
    return sorted(tiers, key=lambda tier: tier.from_days)


def parse_share_class_code(pages: list[str]) -> str:
    for _, line in iter_lines(pages):
        match = _SUB_CODE.search(line)
        if match:
            return match[1]
    raise ReportFormatError("share class code not found in the product summary")
