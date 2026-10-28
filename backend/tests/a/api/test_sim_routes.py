from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wg.api.app import create_app
from wg.contracts.api import ClockView, FaultsView, SimResetResult
from wg.contracts.errors import ErrorBody
from wg.k6.migrate import current_version, discover
from wg.settings import load_settings

pytestmark = pytest.mark.layer("H")

EPOCH = "2026-11-02T09:00:00+08:00"


def client_for(db_path: Path, demo: bool = True) -> TestClient:
    settings = replace(load_settings(), db_path=db_path, demo_mode=demo)
    return TestClient(create_app(settings, frontend_dist=db_path.parent / "no-dist"))


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    client = client_for(tmp_path / "wg.db")
    assert client.post("/sim/reset", json={}).status_code == 200
    return client


def test_startup_migrates_a_new_database(tmp_path: Path):
    client = client_for(tmp_path / "fresh.db")
    with client.app.state.db.read() as conn:
        assert current_version(conn) == len(discover())


def test_reset_loads_the_profile_at_the_epoch(tmp_path: Path):
    client = client_for(tmp_path / "wg.db")
    response = client.post("/sim/reset", json={"profile": "fixture_v1", "variant": "t25"})
    assert response.status_code == 200
    body = SimResetResult.model_validate(response.json())
    assert body.variant == "t25"
    assert body.clock.now.isoformat() == EPOCH and body.clock.day == 0
    with client.app.state.db.read() as conn:
        balance = conn.execute(
            "SELECT balance_fen FROM accounts WHERE account_id = 'acc_main'"
        ).fetchone()[0]
    assert balance == 200000


def test_reset_refuses_an_unknown_variant(client: TestClient):
    response = client.post("/sim/reset", json={"variant": "t99"})
    assert response.status_code == 422
    body = ErrorBody.model_validate(response.json())
    assert body.code == "UNSUPPORTED_REQUEST" and body.message_zh


def test_clock_advances_by_days_and_minutes_then_to_an_instant(client: TestClient):
    step = client.post("/sim/clock/advance", json={"days": 2, "minutes": 30})
    assert step.status_code == 200
    view = ClockView.model_validate(step.json())
    assert view.now.isoformat() == "2026-11-04T09:30:00+08:00" and view.day == 2
    jump = client.post("/sim/clock/advance", json={"to": "2026-11-07T01:00:00Z"})
    assert ClockView.model_validate(jump.json()).day == 5


def test_clock_cannot_move_backwards_except_by_reset(client: TestClient):
    client.post("/sim/clock/advance", json={"days": 3})
    back = client.post("/sim/clock/advance", json={"to": EPOCH})
    assert back.status_code == 422
    assert back.json()["code"] == "UNSUPPORTED_REQUEST"
    assert back.json()["detail"] == {"reason": "clock_backwards"}
    reset = client.post("/sim/reset", json={})
    assert reset.json()["clock"]["day"] == 0


def test_clock_request_takes_one_form(client: TestClient):
    response = client.post("/sim/clock/advance", json={"to": EPOCH, "days": 1})
    assert response.status_code == 422


def test_clock_advance_is_audited(client: TestClient):
    client.post("/sim/clock/advance", json={"minutes": 5})
    with client.app.state.db.read() as conn:
        events = [row[0] for row in conn.execute("SELECT event FROM audit_log ORDER BY audit_id")]
    assert events == ["fixture_loaded", "clock_advanced"]


def test_faults_are_armed_and_listed(client: TestClient):
    response = client.post("/sim/faults", json={"fault": "bank_ack_lost", "count": 2})
    assert response.status_code == 200
    client.post("/sim/faults", json={"fault": "crash_after_commit", "persistent": True})
    view = FaultsView.model_validate(client.post("/sim/faults", json={"fault": "busy"}).json())
    armed = {(f.fault.value, f.remaining_count, f.persistent) for f in view.faults}
    assert armed == {
        ("bank_ack_lost", 2, False),
        ("crash_after_commit", 1, True),
        ("busy", 1, False),
    }
    assert client.post("/sim/faults", json={"fault": "no_such_fault"}).status_code == 422
    assert client.post("/sim/faults", json={"fault": "busy", "count": 0}).status_code == 422


def test_restart_keeps_only_persistent_faults(client: TestClient, tmp_path: Path):
    client.post("/sim/faults", json={"fault": "bank_ack_lost"})
    client.post("/sim/faults", json={"fault": "tick_crash", "persistent": True})
    restarted = client_for(tmp_path / "wg.db")
    with restarted.app.state.db.read() as conn:
        left = [row[0] for row in conn.execute("SELECT fault FROM faults")]
    assert left == ["tick_crash"]


@pytest.mark.parametrize("path", ["/sim/reset", "/sim/clock/advance", "/sim/faults"])
def test_sim_routes_exist_only_in_demo_mode(tmp_path: Path, path: str):
    client = client_for(tmp_path / "wg.db", demo=False)
    assert client.post(path, json={}).status_code == 404
