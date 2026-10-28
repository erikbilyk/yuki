"""Helpers shared by the acceptance modules: audit checks, lock reads and lost-response faults."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from wg.contracts.api import LockView
from wg.contracts.enums import Fault
from wg.contracts.errors import ErrorBody, ErrorCode

from ..conftest import Live
from ..harness.client import WgClient

CRASH_EXIT_STATUS = 137

LOST_RESPONSE_FAULTS = (
    Fault.CRASH_BEFORE_COMMIT,
    Fault.CRASH_AFTER_COMMIT,
    Fault.PAUSE_BEFORE_COMMIT,
    Fault.PAUSE_AFTER_COMMIT,
)
AFTER_COMMIT = (Fault.CRASH_AFTER_COMMIT, Fault.PAUSE_AFTER_COMMIT)
PAUSE = (Fault.PAUSE_BEFORE_COMMIT, Fault.PAUSE_AFTER_COMMIT)


def audit_snapshot(wg: WgClient) -> Counter[str]:
    return Counter(line.model_dump_json() for line in wg.audit().lines)


def new_audit_lines(wg: WgClient, before: Counter[str]) -> list[str]:
    return list((audit_snapshot(wg) - before).elements())


def assert_audited(wg: WgClient, before: Counter[str], code: ErrorCode | None = None) -> None:
    lines = new_audit_lines(wg, before)
    assert lines, "the refusal left no audit line"
    if code is not None:
        assert any(code.value in line for line in lines), f"no audit line names {code}: {lines}"


def assert_refused_without_session(response: httpx.Response) -> None:
    assert response.status_code in (401, 403), response.text
    body = ErrorBody.model_validate_json(response.content)
    assert body.code in (
        ErrorCode.SESSION_EXPIRED,
        ErrorCode.FORBIDDEN_ROUTE,
        ErrorCode.CSRF_FAILED,
    )


def active_locks(wg: WgClient) -> list[LockView]:
    """Active locks as the operator sees them; the client needs an operator session."""
    return list(wg.locks().locks)


def lose_response(live: Live, fault: Fault, send: Callable[[], httpx.Response]) -> None:
    """Send while the fault ends the process: by itself (crash) or by SIGKILL (pause)."""
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(send)
        if fault in PAUSE:
            live.server.wait_for_fault_marker()
            live.server.kill()
        else:
            assert live.server.wait_exit() == CRASH_EXIT_STATUS
        with pytest.raises(httpx.TransportError):
            pending.result(timeout=30)
