"""HTTP sessions of one episode: the customer and the demo panel, each with its own CSRF token."""

from __future__ import annotations

from typing import Any

import httpx

REQUEST_TIMEOUT_S = 60.0
CHAT_TIMEOUT_S = 300.0
CSRF_HEADER = "X-CSRF-Token"


class Session:
    """A cookie jar and the CSRF token of one role against one local backend."""

    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(base_url=base_url, trust_env=False, timeout=REQUEST_TIMEOUT_S)
        self.csrf = ""

    def login(self, handle: str, password: str) -> dict[str, Any]:
        response = self.http.post("/v1/auth/login", json={"handle": handle, "password": password})
        response.raise_for_status()
        entry = response.json()
        self.csrf = entry["csrf_token"]
        return entry

    def alive(self, role: str) -> bool:
        """Whether the session cookie still holds a session of `role`."""
        response = self.http.get("/v1/session")
        return response.status_code == 200 and any(
            entry["role"] == role for entry in response.json()["sessions"]
        )

    def get(self, path: str, **params: Any) -> httpx.Response:
        return self.http.get(path, params=params or None)

    def post(
        self, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> httpx.Response:
        return self.http.post(
            path,
            json=body if body is not None else {},
            headers={CSRF_HEADER: self.csrf},
            timeout=timeout or REQUEST_TIMEOUT_S,
        )

    def close(self) -> None:
        self.http.close()
