"""An in-process app on a fresh database and a browser that holds one session per role."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from wg.api.app import create_app
from wg.contracts.api import SessionEntry
from wg.contracts.enums import Role
from wg.core.k1_session import CSRF_HEADER
from wg.settings import Settings

LOGINS: dict[str, tuple[str, str]] = {
    "cus_main": ("demo-main", "wg-demo-main-2026"),
    "cus_other": ("demo-other", "wg-demo-other-2026"),
    "opr_ops": ("demo-ops", "wg-demo-ops-2026"),
    "opr_demo": ("demo-panel", "wg-demo-panel-2026"),
}


def make_settings(db_path: Path, demo: bool = True) -> Settings:
    return Settings(
        db_path=db_path,
        demo_mode=demo,
        code_hmac_key="test-code-key-" + "0" * 32,
        session_secret="test-session-secret-" + "1" * 32,
        channel_secret="test-channel-secret",
        fault_wait_file=db_path.parent / "fault.wait",
    )


def make_client(db_path: Path, demo: bool = True) -> TestClient:
    settings = make_settings(db_path, demo)
    return TestClient(create_app(settings, frontend_dist=db_path.parent / "no-dist"))


class Browser:
    """A TestClient with the CSRF token of every role it logged in as."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.csrf: dict[Role, str] = {}

    @property
    def db(self):
        return self.client.app.state.db

    def login(self, subject: str) -> SessionEntry:
        handle, password = LOGINS[subject]
        response = self.client.post("/v1/auth/login", json={"handle": handle, "password": password})
        assert response.status_code == 200, response.text
        entry = SessionEntry.model_validate(response.json())
        self.csrf[entry.role] = entry.csrf_token
        return entry

    def post(
        self, path: str, json: Any = None, *, role: Role = Role.CUSTOMER, csrf: bool = True
    ) -> httpx.Response:
        headers = {CSRF_HEADER: self.csrf[role]} if csrf and role in self.csrf else {}
        return self.client.post(path, json=json, headers=headers)

    def get(self, path: str, **params: Any) -> httpx.Response:
        return self.client.get(path, params=params or None)

    def sim(self, path: str, json: Any = None) -> httpx.Response:
        return self.post(path, json, role=Role.DEMO)


@pytest.fixture
def browser(tmp_path: Path) -> Iterator[Browser]:
    """Logged in as the demo panel (after a reset to the base profile) and as cus_main."""
    with make_client(tmp_path / "wg.db") as client:
        browser = Browser(client)
        browser.login("opr_demo")
        assert browser.sim("/sim/reset", {}).status_code == 200
        browser.login("cus_main")
        yield browser
