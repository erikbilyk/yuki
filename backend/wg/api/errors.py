"""Turn a WgError, or a request body that fails validation, into the ErrorBody of SPEC §8.3."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from wg.api.guards import acting_principal
from wg.contracts.errors import ErrorBody, ErrorClass, ErrorCode, WgError
from wg.k6 import audit
from wg.k6.db import Database

MESSAGES_ZH: dict[ErrorCode, str] = {
    ErrorCode.NOT_FOUND: "未找到相关记录",
    ErrorCode.FORBIDDEN_ROUTE: "无权执行此操作",
    ErrorCode.SESSION_EXPIRED: "请先登录",
    ErrorCode.CSRF_FAILED: "请求校验未通过，请刷新页面后重试",
    ErrorCode.UNSUPPORTED_REQUEST: "暂不支持该请求",
    ErrorCode.SINGLE_LIMIT_EXCEEDED: "超过单笔转账限额",
    ErrorCode.DAILY_LIMIT_EXCEEDED: "超过当日转账限额",
    ErrorCode.TIER_CHANGED: "当日累计金额已变化，需要短信验证码",
    ErrorCode.LOCKED_WRITE: "相关操作已暂停，请联系工作人员",
    ErrorCode.READ_SCOPE_DENIED: "超出本次请求的数据范围",
    ErrorCode.APPROVAL_REQUIRED: "请先确认卡片；需要验证码时请重新获取",
    ErrorCode.APPROVAL_EXPIRED: "确认已过期，请重新发起",
    ErrorCode.APPROVAL_REVOKED: "确认已撤销",
    ErrorCode.TERMS_MISMATCH: "确认内容与当前条款不一致，请重新查看卡片",
    ErrorCode.CODE_INVALID: "验证码不正确",
    ErrorCode.CODE_EXPIRED: "验证码已过期，请重新获取",
    ErrorCode.CODE_ATTEMPTS_EXHAUSTED: "验证码错误次数过多，请重新获取",
    ErrorCode.CODE_SCOPE_MISMATCH: "验证码与本次操作不符",
    ErrorCode.GRANT_CONSUMED: "该验证码已使用",
    ErrorCode.SUPERSEDED: "该操作已被新版本取代",
    ErrorCode.ALREADY_SETTLED: "该操作已完成，无法撤销；如需退回请发起新的转账",
    ErrorCode.BANK_REJECTED: "银行未能完成这笔付款，款项未划出",
    ErrorCode.INSUFFICIENT_FUNDS: "账户可用余额不足",
    ErrorCode.INSUFFICIENT_HEADROOM: "超出可自由支配金额，受保护的资金不能用于这笔转账",
    ErrorCode.UNTRUSTED_SOURCE: "收款人或金额来自外部文字，不能作为指令",
    ErrorCode.IDEMPOTENCY_MISMATCH: "同一操作的内容不一致，未执行",
    ErrorCode.UNKNOWN_ACTION_KIND: "暂不支持该操作",
}

CLASS_MESSAGES_ZH: dict[ErrorClass, str] = {
    ErrorClass.EXPECTED: "请求未被接受",
    ErrorClass.TRANSIENT: "系统繁忙，请稍后重试",
    ErrorClass.UNKNOWN: "正在核实状态",
    ErrorClass.UNEXPECTED: "系统异常，操作未执行",
    ErrorClass.INTEGRITY: "系统检测到数据异常，相关操作已暂停",
}


def message_zh(code: ErrorCode) -> str:
    return MESSAGES_ZH.get(code) or CLASS_MESSAGES_ZH[WgError(code).error_class]


def error_response(error: WgError) -> JSONResponse:
    body = ErrorBody(code=error.code, message_zh=message_zh(error.code), detail=error.detail)
    return JSONResponse(body.model_dump(mode="json"), status_code=error.http_status)


def validation_error(request: Request, exc: RequestValidationError) -> WgError:
    """A request that fails its contract model is refused as UNSUPPORTED_REQUEST (422).

    The detail names the offending fields only; the submitted values are not echoed. When a
    session was already admitted, the refusal is audited for that customer.
    """
    fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
    principal = acting_principal(request)
    if principal is not None:
        route = request.scope.get("route")
        ref = (getattr(route, "path", None) or request.url.path)[:64]
        db: Database = request.app.state.db
        db.run_in_uow(
            lambda uow: audit.record(
                uow,
                principal.audit_actor,
                "request_refused",
                customer_id=principal.session.customer_id,
                ref=ref,
                decision=ErrorCode.UNSUPPORTED_REQUEST.value,
                reason="invalid_body",
            ),
            "audit invalid request",
        )
    return WgError(ErrorCode.UNSUPPORTED_REQUEST, {"fields": fields[:20]})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(WgError)
    async def handle_wg_error(_: Request, exc: WgError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # The audit write blocks on the database, so it runs off the event loop.
        return error_response(await run_in_threadpool(validation_error, request, exc))
