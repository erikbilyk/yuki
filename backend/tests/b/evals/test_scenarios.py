"""The scenario files load in the runner's format and say what the scorer can judge."""

from __future__ import annotations

import re
from collections import Counter

import pytest
from evals.runner.schema import (
    SIM_POSTS,
    Allowed,
    ApproveCards,
    EnterCode,
    Say,
    Scenario,
    load_scenario,
)
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
S5_PROFILE = "fixture_s5"
AMOUNT_KINDS = {"TRANSFER", "PAY_BILL", "PURCHASE"}
SMOKE = {"smoke-t1", "smoke-t17", "smoke-t39", "smoke-t60"}
COMPARED = {"T61", "T62", "T64", "T65", "T66", "T67", "T68"}
# Simulator routes that exist only once the chat tools for direct debits are built.
ROUTES_BY_LEVEL = ("/sim/debit-requests",)
S5_LIVE = {
    "grounded-subscriptions-total",
    "colloquial-close-gym-debit",
    "injected-debit-request-text",
}


def directory(name: str) -> list[Scenario]:
    """Scenarios under a folder, except those that post to a simulator route this build lacks."""
    files = sorted((SCENARIOS / name).rglob("*.yaml"))
    missing = [route for route in ROUTES_BY_LEVEL if route not in SIM_POSTS]
    files = [f for f in files if not any(route in f.read_text("utf-8") for route in missing)]
    return [load_scenario(path) for path in files]


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
    live = [s for s in directory("live") if s.profile != S5_PROFILE]
    counts = Counter(s.category for s in live)
    assert set(counts) == CATEGORIES
    assert set(counts.values()) == {3}
    assert all(s.system == "coordinated" for s in live)


def test_the_direct_debit_trials_use_their_profile_and_the_existing_categories() -> None:
    found = [s for s in directory("live") if s.profile == S5_PROFILE]
    assert {s.id for s in found} <= S5_LIVE
    assert all(s.category in CATEGORIES and s.system == "coordinated" for s in found)
    assert {s.case for s in found} <= {"T97", "T101", "T108"}
    # The profile is the only thing that sets them apart: the other trials stay on fixture_v1.
    assert all(s.profile != S5_PROFILE for s in directory("live") if s.id not in S5_LIVE)


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


def test_the_late_redemption_scenario_withholds_the_purchase_code_at_the_last_code_step() -> None:
    (scenario,) = [s for s in directory("comparison") if s.id == "cmp-t65"]
    code_steps = [step.body for step in scenario.steps if isinstance(step.body, EnterCode)]
    assert [bool(step.withhold) for step in code_steps] == [False, False, True]
    (entry,) = code_steps[-1].withhold
    assert (entry.kind, entry.product_id) == ("PURCHASE", "990004")
    assert any(a.kind == "PURCHASE" for a in allow_entries(scenario))
    assert not any(e["kind"] == "PURCHASE" for e in scenario.expectations["effects"])
