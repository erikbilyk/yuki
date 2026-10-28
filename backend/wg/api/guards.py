"""Route access: the single place where the role of SPEC §19.1 is enforced for a route."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Request

from wg.contracts.api import RouteRole
from wg.contracts.errors import ErrorCode, WgError


def require_role(role: RouteRole) -> Callable[[Request], None]:
    """A dependency admitting requests to routes of `role`.

    Demo routes need demo mode. Roles that need a session are refused until session
    checks are added here, so no route can be reached without its check.
    """

    def guard(request: Request) -> None:
        if role is RouteRole.PUBLIC:
            return
        if role is RouteRole.DEMO and request.app.state.settings.demo_mode:
            return
        raise WgError(ErrorCode.FORBIDDEN_ROUTE, {"role": role.value})

    return guard
