"""Turn a WgError into the ErrorBody response of SPEC §8.3."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from wg.contracts.errors import ErrorBody, ErrorClass, ErrorCode, WgError

MESSAGES_ZH: dict[ErrorCode, str] = {
    ErrorCode.NOT_FOUND: "未找到相关记录",
    ErrorCode.FORBIDDEN_ROUTE: "无权执行此操作",
    ErrorCode.UNSUPPORTED_REQUEST: "暂不支持该请求",
}

CLASS_MESSAGES_ZH: dict[ErrorClass, str] = {
    ErrorClass.EXPECTED: "请求未被接受",
    ErrorClass.TRANSIENT: "系统繁忙，请稍后重试",
    ErrorClass.UNKNOWN: "正在核实状态",
    ErrorClass.UNEXPECTED: "系统异常，操作未执行",
    ErrorClass.INTEGRITY: "系统检测到数据异常，相关操作已暂停",
}


def message_zh(error: WgError) -> str:
    return MESSAGES_ZH.get(error.code) or CLASS_MESSAGES_ZH[error.error_class]


def error_response(error: WgError) -> JSONResponse:
    body = ErrorBody(code=error.code, message_zh=message_zh(error), detail=error.detail)
    return JSONResponse(body.model_dump(mode="json"), status_code=error.http_status)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(WgError)
    async def handle_wg_error(_: Request, exc: WgError) -> JSONResponse:
        return error_response(exc)
