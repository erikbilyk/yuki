"""Operator routes, restart recovery at startup and the simulated crash, over HTTP in process."""

from pathlib import Path

import httpx
import pytest

from wg.contracts.api import (
    AuditView,
    ConfirmResult,
    LocksView,
    LockView,
    PlansView,
    ReconcileResult,
)
from wg.contracts.enums import ActionStatus, LockScope, OperationStatus, Role
from wg.contracts.errors import ErrorCode
from wg.k6.gateway import CRASH_EXIT_STATUS
from wg.services.sim import SimService

from .conftest import Browser, make_client
from .test_s1_routes import card_for, confirm, draft, error

pytestmark = pytest.mark.layer("H")


def locks(browser: Browser) -> LocksView:
    response = browser.get("/v1/operator/locks")
    assert response.status_code == 200, response.text
    return LocksView.model_validate(response.json())


def operator_post(browser: Browser, path: str, body: dict) -> httpx.Response:
    return browser.post(path, body, role=Role.OPERATOR)


def fail_three_times(browser: Browser, card) -> None:
    assert browser.sim("/sim/faults", {"fault": "unexpected", "count": 3}).status_code == 200
    for _ in range(3):
        response = confirm(browser, card)
        assert response.status_code == 500 and error(response).code is ErrorCode.UNEXPECTED_FAILURE


@pytest.mark.case("T13")
def test_operator_routes_need_an_operator_session(browser: Browser):
    assert browser.get("/v1/operator/locks").status_code == 401
    response = browser.post("/v1/operator/reconcile", {})
    assert response.status_code == 401
    browser.login("opr_ops")
    assert (
        browser.post("/v1/operator/reconcile", {}, role=Role.OPERATOR, csrf=False).status_code
        == 403
    )
    assert browser.get("/v1/operator/locks").status_code == 200


@pytest.mark.case("T13")
def test_breaker_lock_reconcile_and_unlock(browser: Browser):
    created = draft(browser, "pay_mom", 50_000)
    card = card_for(browser, created.logical_action_id)
    browser.login("opr_ops")

    fail_three_times(browser, card)
    (lock,) = locks(browser).locks
    assert lock.scope is LockScope.PLAN and lock.reason == "breaker: 3 unexpected failures"
    refused = confirm(browser, card)
    assert refused.status_code == 423 and error(refused).code is ErrorCode.LOCKED_WRITE

    reconciled = operator_post(browser, "/v1/operator/reconcile", {"customer_id": "cus_main"})
    assert reconciled.status_code == 200, reconciled.text
    assert ReconcileResult.model_validate(reconciled.json()).checked == 0

    blank = operator_post(browser, f"/v1/operator/locks/{lock.lock_id}/unlock", {"reason": " "})
    assert blank.status_code == 422 and error(blank).code is ErrorCode.UNSUPPORTED_REQUEST
    assert len(locks(browser).locks) == 1

    reason = "已核对全部操作，解除锁定"
    cleared = operator_post(
        browser, f"/v1/operator/locks/{lock.lock_id}/unlock", {"reason": reason}
    )
    assert cleared.status_code == 200, cleared.text
    view = LockView.model_validate(cleared.json())
    assert view.clear_reason == reason and view.cleared_by == "opr_ops"
    assert locks(browser).locks == ()

    audit = AuditView.model_validate(browser.get("/v1/operator/audit").json())
    line = next(line for line in audit.lines if line.event == "lock_cleared")
    assert line.actor.value == "operator" and line.reason == reason
    refused_line = next(line for line in audit.lines if line.event == "unlock_refused")
    assert refused_line.decision == ErrorCode.UNSUPPORTED_REQUEST.value
    # Nothing ran because of the unlock.
    with browser.db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM operations WHERE logical_action_id = ?", (created.logical_action_id,)
        ).fetchall()
    assert rows == []


def test_reconcile_resolves_an_unknown_outcome_under_a_lock(browser: Browser):
    created = draft(browser, "pay_mom", 30_000)
    card = card_for(browser, created.logical_action_id)
    browser.sim("/sim/faults", {"fault": "bank_ack_lost"})
    unknown = confirm(browser, card)
    assert unknown.status_code == 202
    operation_id = ConfirmResult.model_validate(unknown.json()).operation.operation_id
    browser.login("opr_ops")

    response = operator_post(browser, "/v1/operator/reconcile", {"customer_id": "cus_main"})

    result = ReconcileResult.model_validate(response.json())
    assert (result.checked, result.resolved) == (1, 1)
    assert result.operations[0].operation_id == operation_id
    assert result.operations[0].status is OperationStatus.SETTLED


def test_single_plans_are_not_listed_and_cards_show_no_plan(browser: Browser):
    created = draft(browser, "pay_mom", 10_000)
    card = card_for(browser, created.logical_action_id)
    assert card.plan_id is None

    result = ConfirmResult.model_validate(confirm(browser, card).json())

    assert result.plan_id is None and result.action_status is ActionStatus.SETTLED
    assert PlansView.model_validate(browser.get("/v1/plans").json()).plans == ()


@pytest.mark.case("R4")
def test_startup_reconciles_an_unknown_outcome(tmp_path: Path):
    with make_client(tmp_path / "wg.db") as client:
        browser = Browser(client)
        browser.login("opr_demo")
        browser.sim("/sim/reset", {})
        browser.login("cus_main")
        created = draft(browser, "pay_mom", 30_000)
        card = card_for(browser, created.logical_action_id)
        browser.sim("/sim/faults", {"fault": "bank_ack_lost"})
        unknown = ConfirmResult.model_validate(confirm(browser, card).json()).operation
        assert unknown is not None and unknown.status is OperationStatus.UNKNOWN

    with make_client(tmp_path / "wg.db") as restarted:
        with restarted.app.state.db.read() as conn:
            row = conn.execute(
                "SELECT status FROM operations WHERE operation_id = ?", (unknown.operation_id,)
            ).fetchone()
    assert row["status"] == OperationStatus.SETTLED.value


def test_sim_crash_ends_the_process(browser: Browser):
    exits: list[int] = []
    app = browser.client.app
    sim = SimService(app.state.db, app.state.coordinator, exit_process=exits.append)

    sim.crash()

    assert exits == [CRASH_EXIT_STATUS]
    with app.state.db.read() as conn:
        events = [row[0] for row in conn.execute("SELECT event FROM audit_log")]
    assert events[-1] == "sim_crash"


def test_sim_crash_is_a_demo_route(tmp_path: Path):
    with make_client(tmp_path / "wg.db", demo=False) as client:
        assert client.post("/sim/crash").status_code == 404
