from pathlib import Path

import pytest
import yaml
from evals.runner.run import plan_jobs
from evals.runner.schema import (
    AdvanceTo,
    ApproveCards,
    Say,
    Scenario,
    WaitForStatus,
    load_scenario,
    load_scenarios,
)
from pydantic import ValidationError

from wg.settings import REPO_ROOT

EXAMPLES = REPO_ROOT / "evals" / "runner" / "examples"


def scenario(**overrides) -> Scenario:
    raw = {
        "id": "s1",
        "category": "ambiguity",
        "steps": [{"say": "hello"}],
    } | overrides
    return Scenario.model_validate(raw)


def test_example_loads_with_every_step_typed():
    loaded = load_scenario(EXAMPLES / "dry_run_transfer.yaml")
    kinds = [step.kind for step in loaded.steps]
    assert kinds == ["say", "approve_cards", "enter_code", "wait_for_status"]
    assert isinstance(loaded.steps[0].body, Say)
    assert isinstance(loaded.steps[1].body, ApproveCards)
    assert Path(loaded.stub_script or "").is_file()


def test_every_step_kind_parses():
    loaded = scenario(
        steps=[
            {"say": {"text": "hi"}},
            {"approve_cards": {"allow": [{"kind": "TRANSFER", "amount_fen": 100}]}},
            {"enter_code": {}},
            {"advance_to": {"days": 2}},
            {"advance_to": {"to": "2026-11-04T09:00:00+08:00"}},
            {"kill_restart": {"how": "crash"}},
            {"fault": {"fault": "bank_ack_lost"}},
            {"wait_for_status": {"plan": "last", "status": ["completed"]}},
        ]
    )
    assert len(loaded.steps) == 8
    assert isinstance(loaded.steps[3].body, AdvanceTo)
    assert isinstance(loaded.steps[7].body, WaitForStatus)


@pytest.mark.parametrize(
    "step",
    [
        {"say": "a", "enter_code": {}},
        {"unknown_step": {}},
        {"advance_to": {}},
        {"advance_to": {"to": "2026-11-04T09:00:00+08:00", "days": 1}},
        {"wait_for_status": {"status": ["SETTLED"]}},
        {"approve_cards": {"allow": []}},
        {"approve_cards": {"allow": [{"kind": "TRANSFER", "amount_fen": 1, "max_amount_fen": 2}]}},
        {"approve_cards": {"allow": [{"kind": "PLAN"}]}},
        {"approve_cards": {"allow": [{"kind": "TRANSFER", "nodes": [{"kind": "TRANSFER"}]}]}},
        {"say": {"text": "a", "extra": 1}},
    ],
)
def test_invalid_steps_are_rejected(step):
    with pytest.raises(ValidationError):
        scenario(steps=[step])


def test_unknown_fields_and_categories_are_rejected():
    with pytest.raises(ValidationError):
        scenario(colour="red")
    with pytest.raises(ValidationError):
        scenario(category="whatever")


def test_duplicate_ids_are_refused(tmp_path):
    for name in ("a", "b"):
        raw = {"id": "same", "category": "ambiguity", "steps": [{"say": name}]}
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_scenarios(tmp_path)


def test_jobs_run_comparison_on_both_systems_interleaved():
    live = scenario(id="live1")
    comparison_a = scenario(id="cmp_a", category="comparison", system="flat")
    comparison_b = scenario(id="cmp_b", category="comparison")
    everything = [live, comparison_a, comparison_b]

    live_jobs = plan_jobs("live", everything, 2)
    assert [(j.scenario.id, j.system, j.repeat) for j in live_jobs] == [
        ("live1", "coordinated", 1),
        ("live1", "coordinated", 2),
    ]
    compared = plan_jobs("comparison", everything, 3)
    assert len(compared) == 2 * 2 * 3
    assert {j.system for j in compared} == {"coordinated", "flat"}
    assert [j.repeat for j in compared[:4]] == [1, 1, 1, 1]


def test_backend_environment_isolates_the_episode(tmp_path, monkeypatch):
    from evals.runner.backend import backend_environment

    env_file = tmp_path / "model.env"
    env_file.write_text(
        "WG_LLM_PROVIDER=deepseek\nWG_LLM_API_KEY=not-a-real-key\nWG_DB_PATH=/elsewhere/wg.db\n"
        "WG_CODE_HMAC_KEY=fixed-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WG_LLM_MODEL", "from-process")

    live = backend_environment(
        tmp_path, 4321, env_file=env_file, stub_script=None, force_stub=False, spend_cap_cny="3"
    )
    assert live["WG_DB_PATH"] == str(tmp_path / "wg.db")
    assert live["WG_PORT"] == "4321" and live["WG_DEMO_MODE"] == "1"
    assert live["WG_LLM_PROVIDER"] == "deepseek" and live["WG_LLM_MODEL"] == "from-process"
    assert live["WG_CODE_HMAC_KEY"] == "fixed-key"
    assert len(live["WG_SESSION_SECRET"]) == 64
    assert live["WG_LLM_SPEND_CAP_CNY"] == "3"

    stub = backend_environment(
        tmp_path, 4321, env_file=env_file, stub_script="/x.yml", force_stub=True, spend_cap_cny=None
    )
    assert stub["WG_LLM_PROVIDER"] == "stub" and stub["WG_LLM_API_KEY"] == ""
    assert stub["WG_LLM_STUB_SCRIPT"] == "/x.yml"
