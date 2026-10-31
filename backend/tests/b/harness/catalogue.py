"""The case catalogue: priorities, sets, gates, layers and applicability of every case."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CATALOGUE_PATH = Path(__file__).resolve().parent / "catalogue.yaml"

# Layers pytest runs; U (browser) and L (live model) run elsewhere.
HARNESS_LAYERS = ("D", "H", "K", "S")
ALL_LAYERS = HARNESS_LAYERS + ("U", "L")
GATE_ORDER = ("G1", "G2", "G3", "G4")
SIDE_GATES = ("GS5",)
CORE_SETS = ("S1", "S2", "S3", "S6")


class CatalogueError(ValueError):
    """The catalogue file is malformed or a test names an unknown case."""


@dataclass(frozen=True)
class Case:
    case_id: str
    priority: str | None
    set: str
    title: str
    gate: str | None
    layers: tuple[str, ...]
    layer_gates: dict[str, str | None] = field(default_factory=dict)
    applicable: str = "always"
    reason: str | None = None

    def gate_for(self, layer: str) -> str | None:
        """The earliest gate that requires this case at `layer`."""
        return self.layer_gates.get(layer, self.gate)

    def is_applicable(self, features: Iterable[str]) -> bool:
        if self.applicable == "always":
            return True
        if self.applicable == "never":
            return False
        return self.applicable.removeprefix("feature:") in set(features)

    @property
    def is_core(self) -> bool:
        return self.set in CORE_SETS


@dataclass(frozen=True)
class Catalogue:
    cases: dict[str, Case]
    sets: dict[str, dict[str, str]]

    def __getitem__(self, case_id: str) -> Case:
        try:
            return self.cases[case_id]
        except KeyError:
            raise CatalogueError(f"unknown case {case_id!r}") from None

    def __contains__(self, case_id: object) -> bool:
        return case_id in self.cases

    def required_pairs(self, gate: str, features: Iterable[str]) -> set[tuple[str, str]]:
        """(case, layer) pairs the gate requires at harness layers, for applicable cases.

        A case with no listed layers is required at whichever layer its tests use; it is
        returned with the layer "*".
        """
        reached = _gates_up_to(gate)
        features = tuple(features)
        pairs: set[tuple[str, str]] = set()
        for case in self.cases.values():
            if not case.is_applicable(features):
                continue
            if not case.layers:
                if case.gate in reached:
                    pairs.add((case.case_id, "*"))
                continue
            for layer in case.layers:
                if layer in HARNESS_LAYERS and case.gate_for(layer) in reached:
                    pairs.add((case.case_id, layer))
        return pairs

    def external_pairs(self, gate: str | None) -> set[tuple[str, str]]:
        """(case, layer) pairs at U or L that the gate requires; this harness does not run them."""
        reached = _gates_up_to(gate) if gate else set(GATE_ORDER) | set(SIDE_GATES)
        return {
            (case.case_id, layer)
            for case in self.cases.values()
            for layer in case.layers
            if layer not in HARNESS_LAYERS and case.gate_for(layer) in reached
        }


def _gates_up_to(gate: str) -> set[str]:
    if gate in SIDE_GATES:
        return {gate}
    if gate not in GATE_ORDER:
        raise CatalogueError(f"unknown gate {gate!r}")
    return set(GATE_ORDER[: GATE_ORDER.index(gate) + 1])


def load_catalogue(path: Path = CATALOGUE_PATH) -> Catalogue:
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    sets = raw["sets"]
    known_gates: set[str | None] = {*GATE_ORDER, *SIDE_GATES, None}
    cases: dict[str, Case] = {}
    for case_id, entry in raw["cases"].items():
        case = Case(
            case_id=case_id,
            priority=entry.get("priority"),
            set=entry["set"],
            title=entry["title"],
            gate=entry.get("gate"),
            layers=tuple(entry.get("layers", ())),
            layer_gates=dict(entry.get("layer_gates", {})),
            applicable=entry.get("applicable", "always"),
            reason=entry.get("reason"),
        )
        _validate(case, sets, known_gates)
        cases[case_id] = case
    return Catalogue(cases=cases, sets=sets)


def _validate(case: Case, sets: dict[str, object], known_gates: set[str | None]) -> None:
    problems = []
    if case.set not in sets:
        problems.append(f"unknown set {case.set}")
    if case.priority not in ("P0", "P1", "P2", None):
        problems.append(f"unknown priority {case.priority}")
    if case.gate not in known_gates:
        problems.append(f"unknown gate {case.gate}")
    for layer in case.layers:
        if layer not in ALL_LAYERS:
            problems.append(f"unknown layer {layer}")
    for layer, gate in case.layer_gates.items():
        if layer not in case.layers or gate not in known_gates:
            problems.append(f"bad layer gate {layer}: {gate}")
    if case.applicable not in ("always", "never") and not case.applicable.startswith("feature:"):
        problems.append(f"bad applicability {case.applicable}")
    if case.applicable == "never" and not case.reason:
        problems.append("a case that is never applicable needs a reason")
    if problems:
        raise CatalogueError(f"{case.case_id}: {'; '.join(problems)}")
