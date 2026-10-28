import pytest
from pydantic import ValidationError

from wg.contracts.errors import (
    ERROR_CLASS,
    HTTP_STATUS,
    ErrorBody,
    ErrorClass,
    ErrorCode,
    WgError,
)

# SPEC §8.3, by class.
SPEC_CODES = {
    ErrorClass.EXPECTED: """
        NOT_FOUND CSRF_FAILED SESSION_EXPIRED FORBIDDEN_ROUTE INVALID_AMOUNT INVALID_DECIMAL
        AMOUNT_MISMATCH AMBIGUOUS_PAYEE MISSING_SLOT UNTRUSTED_SOURCE UNSUPPORTED_REQUEST
        SINGLE_LIMIT_EXCEEDED DAILY_LIMIT_EXCEEDED TIER_CHANGED LOCKED_WRITE READ_SCOPE_DENIED
        INSUFFICIENT_FUNDS INSUFFICIENT_HEADROOM UNSETTLED_FUNDS STALE_SNAPSHOT DATA_INCOMPLETE
        FEE_CAP TURNOVER_CAP TARGET_UNVERIFIED EXACT_ORDERS_ONLY BILL_INCREASE_OVER_LIMIT
        RETENTION_OVER_LIMIT PURCHASE_BELOW_MIN DEPENDENCY_FAILED APPROVAL_REQUIRED
        APPROVAL_EXPIRED APPROVAL_REVOKED PLAN_APPROVAL_EXPIRED TERMS_MISMATCH CODE_INVALID
        CODE_EXPIRED CODE_ATTEMPTS_EXHAUSTED CODE_SCOPE_MISMATCH GRANT_CONSUMED SUPERSEDED
        BILL_NOT_PAYABLE PAYEE_MISMATCH AMOUNT_OVER_UNPAID NOT_IN_PLAN IDEMPOTENCY_MISMATCH
        PLAN_CYCLE UNKNOWN_ACTION_KIND UNFUNDED_DEPENDENCY DUPLICATE_INTENT
        NOT_BEFORE_AFTER_EXPIRY PLAN_UNFUNDED ALREADY_SETTLED
    """,
    ErrorClass.TRANSIENT: "BUSY_RETRY PROVIDER_TIMEOUT PROBE_TIMEOUT",
    ErrorClass.UNKNOWN: "OUTCOME_UNKNOWN",
    ErrorClass.UNEXPECTED: "UNEXPECTED_FAILURE",
    ErrorClass.INTEGRITY: "INTEGRITY_VIOLATION",
}

SPEC_STATUS = {
    "NOT_FOUND": 404,
    "CSRF_FAILED": 403,
    "SESSION_EXPIRED": 401,
    "FORBIDDEN_ROUTE": 403,
    "INVALID_AMOUNT": 422,
    "INVALID_DECIMAL": 422,
    "LOCKED_WRITE": 423,
    "BUSY_RETRY": 503,
    "PROVIDER_TIMEOUT": 503,
    "PROBE_TIMEOUT": 503,
    "OUTCOME_UNKNOWN": 202,
    "UNEXPECTED_FAILURE": 500,
    "INTEGRITY_VIOLATION": 500,
}


def test_error_codes_equal_the_spec():
    spec = {code for codes in SPEC_CODES.values() for code in codes.split()}
    assert {code.value for code in ErrorCode} == spec


@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_code_has_a_class_and_a_status(code):
    assert code in ERROR_CLASS
    assert code in HTTP_STATUS
    assert code.value in SPEC_CODES[ERROR_CLASS[code]].split()


def test_statuses_follow_the_spec():
    for name, status in SPEC_STATUS.items():
        assert HTTP_STATUS[ErrorCode(name)] == status
    for code, error_class in ERROR_CLASS.items():
        if error_class is ErrorClass.EXPECTED and code.value not in SPEC_STATUS:
            assert HTTP_STATUS[code] in (409, 422), code


def test_wg_error_exposes_class_and_status():
    error = WgError(ErrorCode.LOCKED_WRITE, {"lock_id": "lck_1"})
    assert error.error_class is ErrorClass.EXPECTED
    assert error.http_status == 423
    assert error.detail == {"lock_id": "lck_1"}
    assert WgError(ErrorCode.INTEGRITY_VIOLATION).error_class is ErrorClass.INTEGRITY
    assert WgError(ErrorCode.NOT_FOUND).detail == {}


def test_error_body_shape():
    body = ErrorBody(code=ErrorCode.NOT_FOUND, message_zh="未找到")
    assert body.model_dump(mode="json") == {
        "code": "NOT_FOUND",
        "message_zh": "未找到",
        "detail": {},
    }
    with pytest.raises(ValidationError):
        ErrorBody(code="NOT_A_CODE", message_zh="x")
    with pytest.raises(ValidationError):
        ErrorBody(code=ErrorCode.NOT_FOUND, message_zh="x", trace="secret")
