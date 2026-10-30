"""The scenario files load in the runner's format and say what the scorer can judge."""

from __future__ import annotations

import re
from collections import Counter

import pytest
from evals.runner.schema import Allowed, ApproveCards, Say, Scenario, load_scenarios
from evals.score.checks import KNOWN_KEYS
from evals.score.effects import validate

from wg.settings import REPO_ROOT

SCENARIOS = REPO_ROOT / "evals" / "scenarios"
CATEGORIES = {
    "ambiguity",
    "missing_slots",
    "multi_step",
    "injected_text",
    "stale_data",
    "grounded_explanation",
    "colloquial",
    "out_of_scope",
}
AMOUNT_KINDS = {"TRANSFER", "PAY_BILL", "PURCHASE"}
SMOKE = {"smoke-t1", "smoke-t17", "smoke-t39", "smoke-t60"}
COMPARED = {"T64", "T66", "T67", "T68"}


def directory(name: str) -> list[Scenario]:
    return load_scenarios(SCENARIOS / name)


def allow_entries(scenario: Scenario) -> list[Allowed]:
    found: list[Allowed] = []
    for step in scenario.steps:
        if isinstance(step.body, ApproveCards):
            for entry in step.body.allow:
                found.append(entry)
                found.extend(entry.nodes or ())
    return found


def customer_texts(scenario: Scenario) -> list[str]:
    return [step.body.text for step in scenario.steps if isinstance(step.body, Say)]


ALL = directory(".")


def test_every_file_loads_and_ids_are_unique() -> None:
    assert len({s.id for s in ALL}) == len(ALL)


def test_live_trials_stay_within_three_per_category() -> None:
    live = directory("live")
    counts = Counter(s.category for s in live)
    assert set(counts) == CATEGORIES
    assert max(counts.values()) <= 3
    assert all(s.system == "coordinated" for s in live)


def test_smoke_scenarios_are_the_four_agreed_ones() -> None:
    smoke = directory("smoke")
    assert {s.id for s in smoke} == SMOKE
    assert all(s.expectations["set"] == "smoke" for s in smoke)
    assert {s.case for s in smoke} == {"T1", "T17", "T39", "T60"}


def test_comparison_scenarios_cover_their_cases() -> None:
    comparison = directory("comparison")
    assert {s.case for s in comparison} == COMPARED
    assert all(s.category == "comparison" for s in comparison)


def test_customer_text_includes_english_and_russian_requests() -> None:
    texts = [text for s in directory("live") for text in customer_texts(s)]
    assert any(re.search("[А-Яа-я]", text) for text in texts)
    assert any(re.fullmatch(r"[A-Za-z0-9 ,.]+", text) for text in texts)
    for scenario in ALL:
        if scenario.id not in {"colloquial-russian", "colloquial-english-dad"}:
            assert all(re.search("[一-鿿]", text) for text in customer_texts(scenario)), scenario.id


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_expectations_use_only_keys_the_scorer_reads(scenario: Scenario) -> None:
    expectations = scenario.expectations
    assert set(expectations) - KNOWN_KEYS <= {key for key in expectations if key.startswith("_")}
    validate(expectations.get("effects", []))
    validate(expectations.get("no_actions", []))
    for system in (expectations.get("by_system") or {}).values():
        assert set(system) <= KNOWN_KEYS
        validate(system.get("effects", []))


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_every_amount_in_an_allow_list_is_bounded(scenario: Scenario) -> None:
    for entry in allow_entries(scenario):
        if entry.kind in AMOUNT_KINDS:
            assert entry.amount_fen is not None or entry.max_amount_fen is not None, entry


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_every_expected_effect_can_be_approved(scenario: Scenario) -> None:
    approvable = {entry.kind for entry in allow_entries(scenario)}
    for effect in scenario.expectations.get("effects", []):
        assert effect["kind"] in approvable, (scenario.id, effect["kind"])


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_a_scenario_that_expects_an_effect_approves_it_with_the_same_terms(
    scenario: Scenario,
) -> None:
    allowed = allow_entries(scenario)
    for effect in scenario.expectations.get("effects", []):
        if effect["kind"] not in AMOUNT_KINDS or "amount_fen" not in effect:
            continue
        assert any(
            entry.kind == effect["kind"] and entry.amount_fen == effect["amount_fen"]
            for entry in allowed
        ), (scenario.id, effect)
