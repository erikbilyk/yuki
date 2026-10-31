import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wg.api.app import create_app
from wg.settings import SettingsError, load_settings


def make_client(demo: bool, **kwargs) -> TestClient:
    """An app on a database of its own, so no test touches the working copy's database."""
    db_path = Path(tempfile.mkdtemp(prefix="wg-app-")) / "wg.db"
    settings = replace(load_settings(), demo_mode=demo, db_path=db_path)
    return TestClient(create_app(settings, **kwargs))


def test_healthz_reports_status_build_and_mode():
    body = make_client(demo=True).get("/healthz").json()
    assert body["status"] == "ok"
    assert body["demo"] is True
    assert body["features"] == ["s5"]
    assert isinstance(body["build"], str) and body["build"]


def test_security_headers_on_api_responses():
    response = make_client(demo=False).get("/healthz")
    assert response.headers["content-security-policy"] == (
        "default-src 'self'; frame-ancestors 'none'"
    )
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def test_security_headers_on_errors():
    response = make_client(demo=False).get("/v1/nothing")
    assert response.status_code == 404
    assert response.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_docs_only_in_demo_mode(path):
    assert make_client(demo=True).get(path).status_code == 200
    assert make_client(demo=False).get(path).status_code == 404


def test_start_refuses_empty_secrets():
    settings = replace(load_settings(), code_hmac_key="")
    with pytest.raises(SettingsError, match="WG_CODE_HMAC_KEY"):
        create_app(settings)
    settings = replace(load_settings(), session_secret="")
    with pytest.raises(SettingsError, match="WG_SESSION_SECRET"):
        create_app(settings)


def test_frontend_served_with_spa_fallback(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("outside")
    client = make_client(demo=False, frontend_dist=dist)
    assert client.get("/").text == "<html>app</html>"
    assert client.get("/app.js").text == "console.log(1)"
    assert client.get("/transfers/new").text == "<html>app</html>"
    assert client.get("/../secret.txt").text == "<html>app</html>"
    assert client.get("/v1/unknown").status_code == 404
    assert client.get("/healthz").json()["status"] == "ok"


def test_no_frontend_routes_without_dist(tmp_path: Path):
    client = make_client(demo=False, frontend_dist=tmp_path / "missing")
    assert client.get("/").status_code == 404
