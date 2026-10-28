"""Error codes, their breaker class and HTTP status (SPEC §8.3, §19.3)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import Field, JsonValue, field_validator

from wg.contracts._base import ContractModel


class ErrorClass(StrEnum):
    EXPECTED = "expected"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"
    UNEXPECTED = "unexpected"
    INTEGRITY = "integrity"


class ErrorCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    CSRF_FAILED = "CSRF_FAILED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    FORBIDDEN_ROUTE = "FORBIDDEN_ROUTE"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    INVALID_DECIMAL = "INVALID_DECIMAL"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    AMBIGUOUS_PAYEE = "AMBIGUOUS_PAYEE"
    MISSING_SLOT = "MISSING_SLOT"
    UNTRUSTED_SOURCE = "UNTRUSTED_SOURCE"
    UNSUPPORTED_REQUEST = "UNSUPPORTED_REQUEST"
    SINGLE_LIMIT_EXCEEDED = "SINGLE_LIMIT_EXCEEDED"
    DAILY_LIMIT_EXCEEDED = "DAILY_LIMIT_EXCEEDED"
    TIER_CHANGED = "TIER_CHANGED"
    LOCKED_WRITE = "LOCKED_WRITE"
    READ_SCOPE_DENIED = "READ_SCOPE_DENIED"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    INSUFFICIENT_HEADROOM = "INSUFFICIENT_HEADROOM"
    UNSETTLED_FUNDS = "UNSETTLED_FUNDS"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"
    FEE_CAP = "FEE_CAP"
    TURNOVER_CAP = "TURNOVER_CAP"
    TARGET_UNVERIFIED = "TARGET_UNVERIFIED"
    EXACT_ORDERS_ONLY = "EXACT_ORDERS_ONLY"
    BILL_INCREASE_OVER_LIMIT = "BILL_INCREASE_OVER_LIMIT"
    RETENTION_OVER_LIMIT = "RETENTION_OVER_LIMIT"
    PURCHASE_BELOW_MIN = "PURCHASE_BELOW_MIN"
    DEPENDENCY_FAILED = "DEPENDENCY_FAILED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_REVOKED = "APPROVAL_REVOKED"
    PLAN_APPROVAL_EXPIRED = "PLAN_APPROVAL_EXPIRED"
    TERMS_MISMATCH = "TERMS_MISMATCH"
    CODE_INVALID = "CODE_INVALID"
    CODE_EXPIRED = "CODE_EXPIRED"
    CODE_ATTEMPTS_EXHAUSTED = "CODE_ATTEMPTS_EXHAUSTED"
    CODE_SCOPE_MISMATCH = "CODE_SCOPE_MISMATCH"
    GRANT_CONSUMED = "GRANT_CONSUMED"
    SUPERSEDED = "SUPERSEDED"
    BILL_NOT_PAYABLE = "BILL_NOT_PAYABLE"
    PAYEE_MISMATCH = "PAYEE_MISMATCH"
    AMOUNT_OVER_UNPAID = "AMOUNT_OVER_UNPAID"
    NOT_IN_PLAN = "NOT_IN_PLAN"
    IDEMPOTENCY_MISMATCH = "IDEMPOTENCY_MISMATCH"
    PLAN_CYCLE = "PLAN_CYCLE"
    UNKNOWN_ACTION_KIND = "UNKNOWN_ACTION_KIND"
    UNFUNDED_DEPENDENCY = "UNFUNDED_DEPENDENCY"
    DUPLICATE_INTENT = "DUPLICATE_INTENT"
    NOT_BEFORE_AFTER_EXPIRY = "NOT_BEFORE_AFTER_EXPIRY"
    PLAN_UNFUNDED = "PLAN_UNFUNDED"
    ALREADY_SETTLED = "ALREADY_SETTLED"
    BUSY_RETRY = "BUSY_RETRY"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROBE_TIMEOUT = "PROBE_TIMEOUT"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    UNEXPECTED_FAILURE = "UNEXPECTED_FAILURE"
    INTEGRITY_VIOLATION = "INTEGRITY_VIOLATION"


_C = ErrorCode

# Expected refusals answer 409 when the request conflicts with current state and
# 422 when the request itself is invalid; LOCKED_WRITE answers 423 (SPEC §19.3).
_EXPECTED_STATUS: dict[ErrorCode, int] = {
    _C.NOT_FOUND: 404,
    _C.CSRF_FAILED: 403,
    _C.SESSION_EXPIRED: 401,
    _C.FORBIDDEN_ROUTE: 403,
    _C.INVALID_AMOUNT: 422,
    _C.INVALID_DECIMAL: 422,
    _C.AMOUNT_MISMATCH: 422,
    _C.AMBIGUOUS_PAYEE: 422,
    _C.MISSING_SLOT: 422,
    _C.UNTRUSTED_SOURCE: 422,
    _C.UNSUPPORTED_REQUEST: 422,
    _C.SINGLE_LIMIT_EXCEEDED: 422,
    _C.DAILY_LIMIT_EXCEEDED: 409,
    _C.TIER_CHANGED: 409,
    _C.LOCKED_WRITE: 423,
    _C.READ_SCOPE_DENIED: 422,
    _C.INSUFFICIENT_FUNDS: 409,
    _C.INSUFFICIENT_HEADROOM: 409,
    _C.UNSETTLED_FUNDS: 409,
    _C.STALE_SNAPSHOT: 409,
    _C.DATA_INCOMPLETE: 409,
    _C.FEE_CAP: 422,
    _C.TURNOVER_CAP: 422,
    _C.TARGET_UNVERIFIED: 422,
    _C.EXACT_ORDERS_ONLY: 409,
    _C.BILL_INCREASE_OVER_LIMIT: 409,
    _C.RETENTION_OVER_LIMIT: 409,
    _C.PURCHASE_BELOW_MIN: 422,
    _C.DEPENDENCY_FAILED: 409,
    _C.APPROVAL_REQUIRED: 409,
    _C.APPROVAL_EXPIRED: 409,
    _C.APPROVAL_REVOKED: 409,
    _C.PLAN_APPROVAL_EXPIRED: 409,
    _C.TERMS_MISMATCH: 409,
    _C.CODE_INVALID: 422,
    _C.CODE_EXPIRED: 409,
    _C.CODE_ATTEMPTS_EXHAUSTED: 409,
    _C.CODE_SCOPE_MISMATCH: 409,
    _C.GRANT_CONSUMED: 409,
    _C.SUPERSEDED: 409,
    _C.BILL_NOT_PAYABLE: 409,
    _C.PAYEE_MISMATCH: 422,
    _C.AMOUNT_OVER_UNPAID: 409,
    _C.NOT_IN_PLAN: 422,
    _C.IDEMPOTENCY_MISMATCH: 409,
    _C.PLAN_CYCLE: 422,
    _C.UNKNOWN_ACTION_KIND: 422,
    _C.UNFUNDED_DEPENDENCY: 422,
    _C.DUPLICATE_INTENT: 409,
    _C.NOT_BEFORE_AFTER_EXPIRY: 422,
    _C.PLAN_UNFUNDED: 422,
    _C.ALREADY_SETTLED: 409,
}

_OTHER: dict[ErrorCode, tuple[ErrorClass, int]] = {
    _C.BUSY_RETRY: (ErrorClass.TRANSIENT, 503),
    _C.PROVIDER_TIMEOUT: (ErrorClass.TRANSIENT, 503),
    _C.PROBE_TIMEOUT: (ErrorClass.TRANSIENT, 503),
    _C.OUTCOME_UNKNOWN: (ErrorClass.UNKNOWN, 202),
    _C.UNEXPECTED_FAILURE: (ErrorClass.UNEXPECTED, 500),
    _C.INTEGRITY_VIOLATION: (ErrorClass.INTEGRITY, 500),
}

ERROR_CLASS: Mapping[ErrorCode, ErrorClass] = MappingProxyType(
    {code: ErrorClass.EXPECTED for code in _EXPECTED_STATUS}
    | {code: error_class for code, (error_class, _) in _OTHER.items()}
)

HTTP_STATUS: Mapping[ErrorCode, int] = MappingProxyType(
    _EXPECTED_STATUS | {code: status for code, (_, status) in _OTHER.items()}
)


class ErrorBody(ContractModel):
    """The JSON body of every error response."""

    code: ErrorCode
    message_zh: str
    detail: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("detail")
    @classmethod
    def _no_floats(cls, detail: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _reject_floats(detail)
        return detail


def _reject_floats(value: JsonValue) -> None:
    # Money is fen and Decimals are strings in error details too (T59).
    if isinstance(value, float):
        raise ValueError("error detail must not contain floats")
    if isinstance(value, dict):
        for item in value.values():
            _reject_floats(item)
    elif isinstance(value, list):
        for item in value:
            _reject_floats(item)


class WgError(Exception):
    """A refusal or failure carrying one error code; the API turns it into an ErrorBody."""

    def __init__(self, code: ErrorCode, detail: dict[str, JsonValue] | None = None) -> None:
        super().__init__(code.value)
        self.code = ErrorCode(code)
        self.detail: dict[str, JsonValue] = dict(detail or {})

    @property
    def error_class(self) -> ErrorClass:
        return ERROR_CLASS[self.code]

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]
