"""Route access: the single place where the role and CSRF rule of SPEC §6, §19.1 are enforced.

Every route depends on `route_guard(method, path)`, which reads the route's role and CSRF
flag from the route table, so a route cannot be served without its check.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable

from fastapi import Request

from wg.contracts.api import RouteRole, find_route
from wg.contracts.enums import Role
from wg.contracts.errors import ErrorCode, WgError
from wg.core.k1_session import COOKIE_NAMES, CSRF_HEADER, Principal, SessionService
from wg.k6 import audit
from wg.k6.db import Database

CHANNEL_HEADER = "X-Channel-Secret"

_SESSION_ROLES = {
    RouteRole.CUSTOMER: Role.CUSTOMER,
    RouteRole.OPERATOR: Role.OPERATOR,
    RouteRole.DEMO: Role.DEMO,
}


def principal_for(request: Request, role: Role) -> Principal | None:
    """The valid session of `role` whose cookie this request carries, if any."""
    sessions: SessionService = request.app.state.sessions
    return sessions.resolve(request.cookies.get(COOKIE_NAMES[role]), role)


def require_principal(request: Request, role: Role) -> Principal:
    principal = principal_for(request, role)
    if principal is None:
        raise WgError(ErrorCode.SESSION_EXPIRED, {"role": role.value})
    request.state.principal = principal
    return principal


def check_csrf(request: Request, principal: Principal) -> None:
    """The request must carry the CSRF token of the session it acts with; a refusal is audited."""
    if principal.csrf_matches(request.headers.get(CSRF_HEADER)):
        return
    db: Database = request.app.state.db
    db.run_in_uow(
        lambda uow: audit.record(
            uow,
            principal.audit_actor,
            "csrf_refused",
            customer_id=principal.session.customer_id,
            ref=_route_path(request),
            decision=ErrorCode.CSRF_FAILED.value,
        ),
        "audit csrf refusal",
    )
    raise WgError(ErrorCode.CSRF_FAILED, {"role": principal.role.value})


def require_role(role: RouteRole, *, csrf: bool) -> Callable[[Request], Principal | None]:
    """A dependency admitting requests to routes of `role`; it returns the acting principal.

    Public routes need nothing. Demo routes also need demo mode. A `session` route (logout)
    names its role in the body and checks that session itself. The channel route needs the
    adapter's shared secret.
    """

    def guard(request: Request) -> Principal | None:
        if role in (RouteRole.PUBLIC, RouteRole.SESSION):
            return None
        if role is RouteRole.CHANNEL:
            check_channel_secret(request)
            return None
        if role is RouteRole.DEMO and not request.app.state.settings.demo_mode:
            raise WgError(ErrorCode.FORBIDDEN_ROUTE, {"role": role.value})
        principal = require_principal(request, _SESSION_ROLES[role])
        if csrf:
            check_csrf(request, principal)
        return principal

    return guard


def check_channel_secret(request: Request) -> None:
    expected: str = request.app.state.settings.channel_secret
    presented = request.headers.get(CHANNEL_HEADER, "")
    if not expected or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise WgError(ErrorCode.FORBIDDEN_ROUTE, {"role": RouteRole.CHANNEL.value})


def route_guard(method: str, path: str) -> Callable[[Request], Principal | None]:
    """The guard of one route of the route table, such as ("POST", "/v1/cards/{id}/confirm")."""
    route = find_route(method, path)
    return require_role(route.role, csrf=route.csrf)


def acting_principal(request: Request) -> Principal | None:
    """The principal the guard admitted for this request, if any."""
    return getattr(request.state, "principal", None)


def _route_path(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    return path[:64]
