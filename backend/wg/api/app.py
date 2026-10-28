"""FastAPI application factory."""

from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse

from wg.api import auth, sim
from wg.api.errors import install_error_handlers
from wg.core.k1_session import SessionService
from wg.k6 import faults, fixture
from wg.k6.db import Database, open_database
from wg.k6.migrate import migrate
from wg.services.sim import SimService
from wg.settings import REPO_ROOT, Settings

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

API_PREFIXES = ("/v1", "/sim", "/healthz", "/docs", "/redoc", "/openapi.json")

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def resolve_build_id() -> str:
    """Build ID from var/build_id, else `git describe`, else "unknown"."""
    build_file = REPO_ROOT / "var" / "build_id"
    try:
        text = build_file.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return text
    try:
        result = subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip() or "unknown"


def _is_api_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in API_PREFIXES)


def create_app(settings: Settings, frontend_dist: Path = FRONTEND_DIST) -> FastAPI:
    settings.require_secrets()
    docs = settings.demo_mode
    app = FastAPI(
        title="WealthGuard",
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings
    app.state.db = db = open_database(settings.db_path)
    migrate(db)
    with db.unit_of_work("startup") as uow:
        faults.clear_transient(uow)
    if settings.demo_mode:
        _load_demo_profile(db)
    app.state.sessions = SessionService(db, settings.session_secret)
    app.state.sim = SimService(db)
    install_error_handlers(app)
    build_id = resolve_build_id()

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if _is_api_path(request.url.path):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"status": "ok", "build": build_id, "demo": settings.demo_mode, "features": []}

    app.include_router(auth.router)
    if settings.demo_mode:
        app.include_router(sim.router)

    if (frontend_dist / "index.html").is_file():
        _serve_frontend(app, frontend_dist.resolve())
    return app


def _load_demo_profile(db: Database) -> None:
    """In demo mode a new database starts with the base profile, so the demo panel can log in."""
    with db.read() as conn:
        empty = fixture.is_empty(conn)
    if empty:
        fixture.load_into_empty(db, fixture.read_profile(fixture.DEFAULT_PROFILE))


def _serve_frontend(app: FastAPI, dist: Path) -> None:
    """Serve built files at / and fall back to index.html for client-side routes."""

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str) -> FileResponse:
        if _is_api_path("/" + path):
            raise HTTPException(status_code=404)
        candidate = (dist / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")
