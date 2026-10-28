"""Plain-YAML access to the shared fixture and golden files; no application code."""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "fixture_v1.yaml"
EXPECTED_PATH = Path(__file__).resolve().parent / "expected.yaml"

ELEMENT_KEYS = ("id", "source_txn_id")


def _read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_raw_fixture() -> dict[str, Any]:
    """The fixture exactly as written, variants included."""
    return _read(FIXTURE_PATH)


def load_expected() -> dict[str, Any]:
    return _read(EXPECTED_PATH)


def epoch_date(doc: dict[str, Any]) -> date:
    return datetime.fromisoformat(doc["epoch"]).date()


def date_at(doc: dict[str, Any], offset_days: int) -> date:
    return epoch_date(doc) + timedelta(days=offset_days)


def _element_key(element: Any) -> str | None:
    if isinstance(element, dict):
        for key in ELEMENT_KEYS:
            if key in element:
                return key
    return None


def _step(node: Any, segment: str) -> Any:
    """Descend one path segment: a mapping key or the id of a list element."""
    if isinstance(node, dict):
        return node[segment]
    for element in node:
        key = _element_key(element)
        if key is not None and element[key] == segment:
            return element
    raise KeyError(segment)


def apply_variant(doc: dict[str, Any], name: str) -> dict[str, Any]:
    """A copy of the base profile with the named overlay applied; `variants` is dropped."""
    overlay = doc["variants"][name]
    result = copy.deepcopy({key: value for key, value in doc.items() if key != "variants"})
    for path, value in overlay.get("set", {}).items():
        *parents, leaf = path.split(".")
        node: Any = result
        for segment in parents:
            node = _step(node, segment)
        if not isinstance(node, dict) or leaf not in node:
            raise KeyError(path)
        node[leaf] = copy.deepcopy(value)
    for path, ids in overlay.get("remove", {}).items():
        node = result
        for segment in path.split("."):
            node = _step(node, segment)
        kept = [item for item in node if item[_element_key(item)] not in ids]
        if len(node) - len(kept) != len(ids):
            raise KeyError(f"{path}: unknown id in {ids}")
        node[:] = kept
    return result


def load_fixture(variant: str | None = None) -> dict[str, Any]:
    """The base profile, or the base profile with one variant applied."""
    raw = load_raw_fixture()
    if variant is None:
        return {key: value for key, value in raw.items() if key != "variants"}
    return apply_variant(raw, variant)


def by_id(items: list[dict[str, Any]], key: str = "id") -> dict[str, dict[str, Any]]:
    return {item[key]: item for item in items}
