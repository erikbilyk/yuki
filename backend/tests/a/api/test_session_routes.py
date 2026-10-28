from pathlib import Path

import pytest

from wg.contracts.api import ROUTES, RouteRole, SessionView
from wg.contracts.enums import Role
from wg.contracts.errors import ErrorBody, ErrorCode

from .conftest import Browser, make_client

pytestmark = pytest.mark.layer("H")


def test_login_sets_an_httponly_strict_cookie_per_role(browser: Browser):
    response = browser.client.post(
        "/v1/auth/login", json={"handle": "demo-ops", "password": "wg-demo-ops-2026"}
    )
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("wg_operator=")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    assert {"wg_session", "wg_demo", "wg_operator"} <= set(browser.client.cookies.keys())


def test_session_lists_every_role_of_this_browser(browser: Browser):
    view = SessionView.model_validate(browser.get("/v1/session").json())
    assert {(e.role, e.subject_id) for e in view.sessions} == {
        (Role.DEMO, "opr_demo"),
        (Role.CUSTOMER, "cus_main"),
    }
    assert {e.csrf_token for e in view.sessions} == set(browser.csrf.values())


def test_logout_ends_one_role_only(browser: Browser):
    response = browser.post("/v1/auth/logout", {"role": "demo"}, role=Role.DEMO)
    assert response.status_code == 200
    remaining = SessionView.model_validate(response.json())
    assert [e.role for e in remaining.sessions] == [Role.CUSTOMER]
    listed = SessionView.model_validate(browser.get("/v1/session").json())
    assert [e.role for e in listed.sessions] == [Role.CUSTOMER]
    assert browser.sim("/sim/clock/advance", {"minutes": 1}).status_code == 401


def test_logout_needs_the_token_of_that_role(browser: Browser):
    response = browser.post("/v1/auth/logout", {"role": "demo"}, role=Role.CUSTOMER)
    assert response.status_code == 403 and response.json()["code"] == "CSRF_FAILED"
    assert browser.sim("/sim/clock/advance", {"minutes": 1}).status_code == 200


def test_wrong_password_is_refused(browser: Browser):
    response = browser.client.post(
        "/v1/auth/login", json={"handle": "demo-main", "password": "guess"}
    )
    assert response.status_code == 401
    assert ErrorBody.model_validate(response.json()).code is ErrorCode.SESSION_EXPIRED


def test_sessions_survive_a_reset(browser: Browser):
    assert browser.sim("/sim/reset", {"variant": "t25"}).status_code == 200
    listed = SessionView.model_validate(browser.get("/v1/session").json())
    assert {e.role for e in listed.sessions} == {Role.DEMO, Role.CUSTOMER}
    assert browser.sim("/sim/clock/advance", {"minutes": 1}).status_code == 200


def test_a_new_demo_database_starts_with_the_base_profile(tmp_path: Path):
    with make_client(tmp_path / "wg.db") as client:
        login = client.post(
            "/v1/auth/login", json={"handle": "demo-panel", "password": "wg-demo-panel-2026"}
        )
        assert login.status_code == 200
    with make_client(tmp_path / "plain.db", demo=False) as client:
        login = client.post(
            "/v1/auth/login", json={"handle": "demo-panel", "password": "wg-demo-panel-2026"}
        )
        assert login.status_code == 401


def _mounted(browser: Browser) -> list[tuple[str, str, RouteRole]]:
    """Routes of the route table that the app serves, read from its OpenAPI schema."""
    paths = browser.client.app.openapi()["paths"]
    return [(r.method, r.path, r.role) for r in ROUTES if r.method.lower() in paths.get(r.path, {})]


@pytest.mark.case("T9")
def test_every_mounted_route_refuses_a_request_without_its_session(tmp_path: Path):
    with make_client(tmp_path / "wg.db") as client:
        mounted = _mounted(Browser(client))
        assert ("POST", "/sim/reset", RouteRole.DEMO) in mounted
        for method, path, role in mounted:
            if role in (RouteRole.PUBLIC, RouteRole.SESSION):
                continue
            url = path.replace("{id}", "crd_x")
            response = client.request(method, url, json={})
            assert response.status_code in (401, 403), (method, path, response.text)
            assert response.json()["code"] in ("SESSION_EXPIRED", "FORBIDDEN_ROUTE")


@pytest.mark.case("T9")
def test_simulator_refuses_customer_sessions(tmp_path: Path):
    with make_client(tmp_path / "wg.db") as client:
        customer = Browser(client)
        customer.login("cus_main")
        response = customer.post("/sim/clock/advance", {"days": 1})
        assert response.status_code == 401 and response.json()["code"] == "SESSION_EXPIRED"
