"""Connections and units of work: the only path that writes to the database (SPEC §11).

One write connection per database file and process, serialized by a lock. Every write
runs inside `unit_of_work(reason)`, which issues BEGIN IMMEDIATE and commits or rolls
back. Reads use separate read-only connections opened with `mode=ro`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Protocol, TypeVar

from wg.contracts.errors import ErrorClass, ErrorCode, WgError

log = logging.getLogger("wg.k6")

T = TypeVar("T")

BUSY_TIMEOUT_MS = 5000
BUSY_RETRIES = 3

_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})

_current: ContextVar[UnitOfWork | None] = ContextVar("wg_k6_unit_of_work", default=None)


class NestedTransactionError(RuntimeError):
    """A unit of work was opened while another one is open in the same context."""


class Reader(Protocol):
    """Anything that runs a query: a read-only connection or an open unit of work."""

    def execute(self, sql: str, parameters: Sequence[Any] = ..., /) -> sqlite3.Cursor: ...


class FailurePolicy(Protocol):
    """Decides, inside the failure-recording unit, what a recorded failure leads to."""

    def __call__(self, uow: UnitOfWork, failure: RecordedFailure) -> None: ...


class RecordedFailure(Protocol):
    error_class: ErrorClass
    detail: dict[str, str]
    plan_id: str | None
    logical_action_id: str | None
    breaker_count: int | None


class UnitOfWork:
    """One open write transaction. Repositories receive it to run their statements."""

    def __init__(self, conn: sqlite3.Connection, reason: str) -> None:
        self._conn = conn
        self.reason = reason

    def execute(self, sql: str, parameters: Sequence[Any] = (), /) -> sqlite3.Cursor:
        return self._conn.execute(sql, parameters)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]], /) -> sqlite3.Cursor:
        return self._conn.executemany(sql, rows)


def in_transaction() -> bool:
    """True while a unit of work is open in the current context."""
    return _current.get() is not None


def is_busy(exc: BaseException) -> bool:
    """True for SQLITE_BUSY and SQLITE_LOCKED, including their extended codes."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in _BUSY_CODES


def _configure(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys=ON")


class Database:
    """A SQLite file with its single write connection and read-only connections."""

    def __init__(self, path: Path, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
        self.path = Path(path).resolve()
        self.busy_timeout_ms = busy_timeout_ms
        # Called inside the unit that records a failure; K8 uses it to set locks (SPEC §16).
        self.failure_policy: FailurePolicy | None = None
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _write_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                self.path, isolation_level=None, check_same_thread=False, timeout=0
            )
            _configure(conn, self.busy_timeout_ms)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            self._conn = conn
        return self._conn

    @contextmanager
    def unit_of_work(self, reason: str) -> Iterator[UnitOfWork]:
        """BEGIN IMMEDIATE; commit when the block ends, roll back when it raises."""
        if in_transaction():
            raise NestedTransactionError(f"unit of work {reason!r} opened inside another")
        with self._lock:
            conn = self._write_connection()
            conn.execute("BEGIN IMMEDIATE")
            uow = UnitOfWork(conn, reason)
            token = _current.set(uow)
            try:
                yield uow
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                _current.reset(token)

    def run_in_uow(
        self,
        fn: Callable[[UnitOfWork], T],
        reason: str,
        *,
        plan_id: str | None = None,
        logical_action_id: str | None = None,
    ) -> T:
        """Run `fn` in its own unit of work and return its result.

        SQLITE_BUSY retries the whole unit up to BUSY_RETRIES times, then raises BUSY_RETRY.
        An unexpected or integrity failure is recorded by `record_failure` in a separate
        unit after the rollback, so the evidence survives, and is then re-raised.
        """
        if in_transaction():
            raise NestedTransactionError(f"unit of work {reason!r} opened inside another")
        for attempt in range(BUSY_RETRIES + 1):
            try:
                with self.unit_of_work(reason) as uow:
                    return fn(uow)
            except Exception as exc:
                if is_busy(exc):
                    log.warning("busy reason=%s attempt=%d", reason, attempt + 1)
                    continue
                error_class = _failure_class(exc)
                if error_class is not None:
                    self._record_failure(exc, error_class, plan_id, logical_action_id)
                raise
        raise WgError(ErrorCode.BUSY_RETRY, {"reason": reason, "attempts": BUSY_RETRIES + 1})

    def _record_failure(
        self,
        exc: Exception,
        error_class: ErrorClass,
        plan_id: str | None,
        logical_action_id: str | None,
    ) -> None:
        # Imported here because failures.py builds on this module.
        from wg.k6.failures import record_failure

        try:
            record_failure(
                self,
                error_class=error_class,
                detail=_failure_detail(exc),
                plan_id=plan_id,
                logical_action_id=logical_action_id,
                policy=self.failure_policy,
            )
        except Exception:
            # The original exception is re-raised by the caller; this one is only logged.
            log.exception("failure record not written plan=%s", plan_id)

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A read-only connection (`mode=ro`); it refuses every write."""
        conn = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=0)
        try:
            _configure(conn, self.busy_timeout_ms)
            conn.execute("PRAGMA query_only=ON")
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def _failure_class(exc: Exception) -> ErrorClass | None:
    """The class to record for an exception that escaped a unit, or None to record nothing."""
    if isinstance(exc, WgError):
        if exc.error_class in (ErrorClass.UNEXPECTED, ErrorClass.INTEGRITY):
            return exc.error_class
        return None
    return ErrorClass.UNEXPECTED


def _failure_detail(exc: Exception) -> dict[str, str]:
    detail = {"exception": type(exc).__name__}
    if isinstance(exc, WgError):
        detail["code"] = exc.code.value
        for key in ("reason", "operation_id", "customer_id"):
            value = exc.detail.get(key)
            if isinstance(value, str):
                detail[key] = value
    else:
        detail["message"] = str(exc)[:200]
    return detail


_databases: dict[Path, Database] = {}
_databases_lock = threading.Lock()


def open_database(path: Path) -> Database:
    """The process-wide Database for `path`, so each file has one write connection."""
    key = Path(path).resolve()
    with _databases_lock:
        db = _databases.get(key)
        if db is None:
            db = _databases[key] = Database(key)
        return db
