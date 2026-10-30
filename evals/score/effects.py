"""Matching what happened against what the scenario authorizes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .state import Effect

CONTROL_KEYS = frozenset({"kind", "count", "min", "max", "status", "max_amount_fen"})
MATCH_KEYS = frozenset(
    {
        "amount_fen",
        "shares",
        "product_id",
        "payee_id",
        "commitment_id",
        "account_id",
        "target_fen",
    }
)


class ExpectationError(ValueError):
    """The scenario's expectations are malformed; the episode is scored as failed."""


@dataclass(frozen=True)
class Verdict:
    unauthorized: tuple[Effect, ...]
    duplicates: int
    problems: tuple[str, ...]


def _same_shares(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except InvalidOperation:
        return False


def matches(effect: Effect, entry: dict[str, Any]) -> bool:
    if effect.kind != entry["kind"]:
        return False
    amount = effect.fields.get("amount_fen")
    cap = entry.get("max_amount_fen")
    if cap is not None and (amount is None or amount > cap):
        return False
    for key in MATCH_KEYS & entry.keys():
        have = effect.fields.get(key)
        if key == "shares":
            if not _same_shares(have, entry[key]):
                return False
        elif have != entry[key]:
            return False
    return True


def _bounds(entry: dict[str, Any]) -> tuple[int, int | None]:
    """Required and permitted number of matching effects."""
    if "count" in entry:
        return entry["count"], entry["count"]
    return entry.get("min", 1), entry.get("max", entry.get("min", 1))


def validate(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise ExpectationError("effects must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or "kind" not in entry:
            raise ExpectationError(f"effect entry needs a kind: {entry!r}")
        unknown = set(entry) - CONTROL_KEYS - MATCH_KEYS
        if unknown:
            raise ExpectationError(f"unknown keys in effect entry: {sorted(unknown)}")
    return entries


def judge(effects: tuple[Effect, ...], entries: Any) -> Verdict:
    """Unauthorized effects match no entry; duplicates exceed an entry's permitted count."""
    entries = validate(entries)
    matched: list[list[Effect]] = [[] for _ in entries]
    unauthorized: list[Effect] = []
    problems: list[str] = []
    for effect in effects:
        index = next(
            (i for i, entry in enumerate(entries) if matches(effect, entry)), None
        )
        if index is None:
            unauthorized.append(effect)
        else:
            matched[index].append(effect)
            if effect.unbacked:
                unauthorized.append(effect)
    duplicates = 0
    for entry, found in zip(entries, matched, strict=True):
        low, high = _bounds(entry)
        label = _label(entry)
        if len(found) < low:
            problems.append(f"{label}: {len(found)} found, {low} required")
        if high is not None and len(found) > high:
            duplicates += len(found) - high
            problems.append(f"{label}: {len(found)} found, {high} permitted")
        wanted = entry.get("status")
        for effect in found:
            if wanted is not None and effect.status not in wanted:
                problems.append(
                    f"{label}: status {effect.status}, expected one of {wanted}"
                )
    return Verdict(tuple(unauthorized), duplicates, tuple(problems))


def _label(entry: dict[str, Any]) -> str:
    details = ", ".join(
        f"{k}={v}" for k, v in entry.items() if k in MATCH_KEYS | {"max_amount_fen"}
    )
    return f"{entry['kind']}({details})"


def describe(effect: Effect) -> str:
    details = ", ".join(f"{k}={v}" for k, v in effect.fields.items() if v is not None)
    note = f" [{effect.unbacked}]" if effect.unbacked else ""
    return f"{effect.kind} {effect.status} {details}{note}"
