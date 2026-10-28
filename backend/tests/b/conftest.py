"""Fixtures for acceptance cases: the in-process app (layer D) and subprocess backends (H, K)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wg.api.app import create_app
from wg.contracts.enums import Role

from .harness.client import WgClient, http_client
from .harness.env import harness_settings
from .harness.server import BackendServer


def _ready(client: WgClient) -> WgClient:
    """Log in as demo and as the main customer, then load the base fixture."""
    client.login(Role.DEMO)
    client.reset()
    client.login(Role.CUSTOMER)
    return client


@pytest.fixture
def app_client(tmp_path: Path) -> Iterator[WgClient]:
    """Layer D: the FastAPI app in this process through TestClient, on a fresh database."""
    settings = harness_settings(tmp_path / "app")
    app = create_app(settings)
    with TestClient(app) as http:
        client = WgClient(http, new_http=lambda: TestClient(app), db_path=settings.db_path)
        yield _ready(client)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[BackendServer]:
    """A backend subprocess on a free port with its own database, stopped after the test."""
    backend = BackendServer(tmp_path / "server")
    try:
        backend.start()
        yield backend
    finally:
        backend.stop()


@pytest.fixture
def server_client(server: BackendServer) -> Iterator[WgClient]:
    """Layer H: real HTTP to the subprocess backend."""
    client = WgClient(
        http_client(server.base_url),
        new_http=lambda: http_client(server.base_url),
        db_path=server.db_path,
    )
    try:
        yield _ready(client)
    finally:
        client.close()


@pytest.fixture(
    params=[
        pytest.param("D", marks=pytest.mark.layer("D")),
        pytest.param("H", marks=pytest.mark.layer("H")),
    ]
)
def wg(request: pytest.FixtureRequest) -> WgClient:
    """The same case at layer D and at layer H."""
    name = "app_client" if request.param == "D" else "server_client"
    return request.getfixturevalue(name)


@dataclass
class Live:
    """Layer K: a backend the test kills and restarts, and a client that follows it."""

    server: BackendServer
    client: WgClient

    def restart(self) -> None:
        """Start a new process on the same database and log in again."""
        self.server.restart()
        self.client.relogin()


@pytest.fixture(params=[pytest.param("K", marks=pytest.mark.layer("K"))])
def live(server: BackendServer, server_client: WgClient) -> Live:
    return Live(server=server, client=server_client)
