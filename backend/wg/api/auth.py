"""Session routes: login, logout and the sessions of this browser (SPEC §6)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from wg.api.guards import check_csrf, principal_for, require_principal, route_guard
from wg.contracts.api import LoginRequest, LogoutRequest, SessionEntry, SessionView
from wg.contracts.enums import Role
from wg.core.k1_session import COOKIE_NAMES, SESSION_TTL, Principal, SessionService

router = APIRouter(prefix="/v1")

Public = Annotated[None, Depends(route_guard("POST", "/v1/auth/login"))]
AnySession = Annotated[None, Depends(route_guard("POST", "/v1/auth/logout"))]
SessionRead = Annotated[None, Depends(route_guard("GET", "/v1/session"))]


def _sessions(request: Request) -> SessionService:
    return request.app.state.sessions


def _held_tokens(request: Request) -> dict[Role, str]:
    return {
        role: request.cookies[name]
        for role, name in COOKIE_NAMES.items()
        if name in request.cookies
    }


def _entries(request: Request, skip: Role | None = None) -> SessionView:
    principals: list[Principal] = []
    for role in COOKIE_NAMES:
        principal = principal_for(request, role)
        if principal is not None and role is not skip:
            principals.append(principal)
    service = _sessions(request)
    return SessionView(sessions=tuple(service.entry(p) for p in principals))


@router.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response, _: Public) -> SessionEntry:
    entry, token = _sessions(request).login(body.handle, body.password, _held_tokens(request))
    response.set_cookie(
        COOKIE_NAMES[entry.role],
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
        httponly=True,
        samesite="strict",
    )
    return entry


@router.post("/auth/logout")
def logout(body: LogoutRequest, request: Request, response: Response, _: AnySession) -> SessionView:
    """End the session of one role; the request carries that role's CSRF token."""
    principal = require_principal(request, body.role)
    check_csrf(request, principal)
    _sessions(request).logout(principal)
    response.delete_cookie(COOKIE_NAMES[body.role], path="/", httponly=True, samesite="strict")
    return _entries(request, skip=body.role)


@router.get("/session")
def session(request: Request, _: SessionRead) -> SessionView:
    return _entries(request)
