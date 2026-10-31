"""The customer's audit view: plain zh-CN lines, never hashes, JSON or model reasoning."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from wg.api.errors import message_zh
from wg.api.guards import route_guard
from wg.contracts.api import AuditLine, AuditQuery, AuditView
from wg.contracts.errors import ErrorCode
from wg.core.k1_session import Principal
from wg.k6.audit import AuditRow, list_audit
from wg.k6.db import Database

router = APIRouter(prefix="/v1")

AuditReader = Annotated[Principal, Depends(route_guard("GET", "/v1/audit"))]

EVENT_TEXT: dict[str, str] = {
    "login": "登录",
    "logout": "退出登录",
    "card_opened": "显示了确认卡片",
    "approval_created": "在确认卡片上确认",
    "approval_revoked": "确认已撤销",
    "challenge_issued": "发送了短信验证码",
    "challenge_verified": "短信验证码验证通过",
    "challenge_invalidated": "短信验证码已失效",
    "grant_consumed": "验证授权已用于执行",
    "confirm_refused": "确认未被接受",
    "card_read_refused": "查看卡片未被接受",
    "challenge_refused": "获取验证码未被接受",
    "reissue_refused": "重新获取验证码未被接受",
    "verify_refused": "验证码未通过",
    "csrf_refused": "请求校验未通过",
    "request_refused": "请求格式不符，未被接受",
    "extra_read": "超出范围的数据读取被拒绝",
    "explanation_replaced": "回答中的数字或状态未通过核对，已改为系统说明",
    "clock_advanced": "模拟时间前进",
    "lock_set": "操作已暂停",
    "lock_cleared": "操作暂停已解除",
    "draft_created": "创建了草稿",
    "draft_refused": "草稿未被接受",
    "approval_request_refused": "显示确认卡片未被接受",
    "action_cancelled": "操作已取消",
    "cancel_refused": "取消未被接受",
    "tier_changed": "当日累计金额已变化，需要短信验证码",
    "operation_submitted": "已提交执行",
    "operation_settled": "已完成",
    "operation_rejected": "银行未受理，资金未转出",
    "operation_unknown": "执行结果待核实",
    "operation_reconciled": "执行结果已核实",
    "incoming_payment": "收到转入款项",
    "awaiting_code": "等待您操作：需要短信验证码",
    "node_ready": "计划步骤已就绪",
    "node_paused": "计划步骤已暂停",
    "node_cancelled": "计划步骤已取消",
    "plan_status": "计划状态已更新",
    "plan_stopped": "计划已停止",
    "unlock_refused": "解除暂停未被接受",
    "operator_reconcile": "运营人员核实了执行结果",
    "sim_crash": "模拟服务中断",
    "debit_request_received": "收到商户扣款请求",
    "debit_request_refused": "商户扣款请求格式不正确，未受理",
    "debit_request_repeat_mismatch": "商户重复请求的内容不一致",
    "debit_request_paid": "扣款请求已支付",
    "debit_request_declined": "已拒绝扣款请求",
    "request_expired": "扣款请求已过回复期限",
}


def _label(value: str | None) -> str | None:
    return value[:64] if value else None


def audit_line(row: AuditRow) -> AuditLine:
    text = EVENT_TEXT.get(row.event, "系统记录")
    if row.decision in ErrorCode.__members__:
        text = f"{text}：{message_zh(ErrorCode(row.decision))}"
    return AuditLine(
        at=row.at,
        actor=row.actor,
        event=row.event[:64],
        ref=_label(row.ref),
        decision=_label(row.decision),
        reason=_label(row.reason),
        text=text[:200],
    )


@router.get("/audit")
def customer_audit(
    request: Request, principal: AuditReader, query: Annotated[AuditQuery, Query()]
) -> AuditView:
    db: Database = request.app.state.db
    with db.read() as conn:
        refs = None
        if query.plan_id is not None:
            refs = {query.plan_id} | {
                row["logical_action_id"]
                for row in conn.execute(
                    "SELECT DISTINCT logical_action_id FROM actions"
                    " WHERE plan_id = ? AND customer_id = ?",
                    (query.plan_id, principal.customer_id),
                )
            }
        rows = list_audit(conn, customer_id=principal.customer_id, refs=refs, limit=query.limit)
    return AuditView(lines=tuple(audit_line(row) for row in rows))
