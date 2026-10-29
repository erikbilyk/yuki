"""The bank simulator: its request book and the ledger (SPEC §7, §12.3–§12.6).

The simulator shares the gateway's database, so a bank call is a set of writes inside the
gateway's unit of work. `bank_requests` is the bank's own record of each payment order by
idempotency key; reconciliation reads only that record. Ledger entries are posted when a
payment settles: a same-bank payment settles in the call that submits it, an other-bank
payment at the next 09:00. Until then the gateway's reservation holds the money. A fund
order settles on its product's calendar (SPEC §15.3) against the simulator's fund
settlement and fee income accounts.

Two fault points live here (SPEC §12.7): `bank_ack_lost` fires after the order and its
effect are recorded, and `settlement_reject` makes the bank refuse a payment at settlement.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from wg.contracts.enums import (
    AuditActor,
    BankRequestStatus,
    Fault,
    OperationEventKind,
    OperationStatus,
)
from wg.contracts.errors import ErrorCode, WgError
from wg.core.hashes import sha256_hex
from wg.k6 import accounts, audit, faults, operations
from wg.k6.db import Reader, UnitOfWork
from wg.k6.operations import OperationRow
from wg.k6.simtime import from_db, now_db, to_db

CLEARING_ACCOUNT = "acc_sys_clearing"
FUND_SETTLEMENT_ACCOUNT = "acc_sys_fund_settlement"
FEE_INCOME_ACCOUNT = "acc_sys_fee_income"
INCOMING_PAYMENT = "INCOMING_PAYMENT"
EXTERNAL_DEBIT = "EXTERNAL_DEBIT"
SETTLEMENT_REJECTED = "settlement_rejected"


class AckLost(Exception):
    """The bank recorded the order and its effect, but its acknowledgement never arrived."""

    def __init__(self, request: BankRequest) -> None:
        super().__init__(request.key)
        self.request = request


@dataclass(frozen=True)
class Posting:
    account_id: str
    amount_fen: int


@dataclass(frozen=True)
class PaymentOrder:
    key: str
    operation_id: str
    account_id: str
    amount_fen: int
    payee_id: str
    settle_due_at: datetime


@dataclass(frozen=True)
class BankRequest:
    key: str
    status: BankRequestStatus
    effect: Mapping[str, Any]
    version: int

    @property
    def rejected(self) -> bool:
        return self.status is BankRequestStatus.REJECTED

    @property
    def settled_at(self) -> datetime | None:
        value = self.effect.get("settled_at")
        return from_db(value) if value else None

    @property
    def settled(self) -> bool:
        return self.settled_at is not None


def _request(row) -> BankRequest:
    return BankRequest(
        key=row["idempotency_key"],
        status=BankRequestStatus(row["status"]),
        effect=json.loads(row["effect_json"]),
        version=row["version"],
    )


def _effect_json(effect: Mapping[str, Any]) -> str:
    return json.dumps(dict(effect), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def find_request(reader: Reader, key: str) -> BankRequest | None:
    row = reader.execute("SELECT * FROM bank_requests WHERE idempotency_key = ?", (key,)).fetchone()
    return None if row is None else _request(row)


def _insert_request(
    uow: UnitOfWork, key: str, status: BankRequestStatus, effect: Mapping[str, Any]
) -> BankRequest:
    uow.execute(
        "INSERT INTO bank_requests (idempotency_key, status, effect_json, created_at)"
        " VALUES (?, ?, ?, ?)",
        (key, status.value, _effect_json(effect), now_db(uow)),
    )
    request = find_request(uow, key)
    assert request is not None
    return request


def _update_request(
    uow: UnitOfWork, request: BankRequest, status: BankRequestStatus, effect: Mapping[str, Any]
) -> BankRequest:
    cursor = uow.execute(
        "UPDATE bank_requests SET status = ?, effect_json = ?, version = version + 1"
        " WHERE idempotency_key = ? AND version = ?",
        (status.value, _effect_json(effect), request.key, request.version),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"bank request {request.key} changed since it was read")
    updated = find_request(uow, request.key)
    assert updated is not None
    return updated


# Ledger


def post_entries(
    uow: UnitOfWork, operation_id: str, postings: Sequence[Posting], at: datetime
) -> None:
    """Post balanced entries for one operation and move the account balances with them."""
    if sum(p.amount_fen for p in postings) != 0:
        # A data invariant: K8 locks the customer (SPEC §16).
        raise WgError(
            ErrorCode.INTEGRITY_VIOLATION, {"operation_id": operation_id, "reason": "ledger_sum"}
        )
    for posting in postings:
        uow.execute(
            "INSERT INTO ledger_entries (operation_id, account_id, amount_fen, posted_at)"
            " VALUES (?, ?, ?, ?)",
            (operation_id, posting.account_id, posting.amount_fen, to_db(at)),
        )
        uow.execute(
            "UPDATE accounts SET balance_fen = balance_fen + ?, version = version + 1"
            " WHERE account_id = ?",
            (posting.amount_fen, posting.account_id),
        )


def ledger_sum(reader: Reader, operation_id: str) -> int:
    row = reader.execute(
        "SELECT COALESCE(SUM(amount_fen), 0) FROM ledger_entries WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    return int(row[0])


# Payments


def submit_payment(
    uow: UnitOfWork, order: PaymentOrder, at: datetime, *, settle_now: bool
) -> BankRequest:
    """Accept a payment order once per key; a same-bank order settles in the same call.

    Raises AckLost, after the order and its effect are written, when `bank_ack_lost` fires.
    """
    request = find_request(uow, order.key)
    if request is not None:
        return request
    request = _insert_request(
        uow,
        order.key,
        BankRequestStatus.EXECUTED,
        {
            "operation_id": order.operation_id,
            "account_id": order.account_id,
            "amount_fen": order.amount_fen,
            "payee_id": order.payee_id,
            "to_account_id": _credit_account(uow, order.payee_id),
            "accepted_at": to_db(at),
            "settle_due_at": to_db(order.settle_due_at),
            "settled_at": None,
            "reason": None,
        },
    )
    if settle_now:
        request = settle_payment(uow, order.key, at)
    if faults.consume(uow, Fault.BANK_ACK_LOST):
        raise AckLost(request)
    return request


def settle_payment(uow: UnitOfWork, key: str, at: datetime) -> BankRequest:
    """Settle an accepted order: post its ledger entries, or reject it when the fault fires.

    A settled or rejected order is returned unchanged, so settling twice changes nothing.
    """

    def postings(effect: Mapping[str, Any]) -> tuple[Posting, ...]:
        amount = int(effect["amount_fen"])
        credit = effect.get("to_account_id") or CLEARING_ACCOUNT
        return (Posting(str(effect["account_id"]), -amount), Posting(str(credit), amount))

    return _settle(uow, key, at, postings)


def _credit_account(reader: Reader, payee_id: str) -> str | None:
    """The customer's own account behind a payee; other payees are paid through clearing."""
    account = accounts.own_account(reader, payee_id)
    return None if account is None else account.account_id


def _settle(
    uow: UnitOfWork,
    key: str,
    at: datetime,
    postings: Callable[[Mapping[str, Any]], Sequence[Posting]],
) -> BankRequest:
    request = find_request(uow, key)
    if request is None:
        raise LookupError(f"no bank request {key}")
    if request.rejected or request.settled:
        return request
    effect = dict(request.effect)
    if faults.consume(uow, Fault.SETTLEMENT_REJECT):
        effect.update(reason=SETTLEMENT_REJECTED, rejected_at=to_db(at))
        return _update_request(uow, request, BankRequestStatus.REJECTED, effect)
    post_entries(uow, str(effect["operation_id"]), postings(effect), at)
    effect["settled_at"] = to_db(at)
    return _update_request(uow, request, BankRequestStatus.EXECUTED, effect)


# Fund orders


class FundSide(StrEnum):
    REDEEM = "redeem"
    PURCHASE = "purchase"


@dataclass(frozen=True)
class FundOrder:
    """A redemption or purchase at a NAV known before the order (the simulator's terms).

    `cash_fen` is the effect on the customer's account at settlement: the net proceeds of a
    redemption (positive) or the amount of a purchase (negative).
    """

    key: str
    operation_id: str
    account_id: str
    product_id: str
    side: FundSide
    shares: Decimal
    nav: Decimal
    gross_fen: int
    fee_fen: int
    cash_fen: int
    settle_due_at: datetime


def submit_fund_order(
    uow: UnitOfWork, order: FundOrder, at: datetime, *, settle_now: bool
) -> BankRequest:
    """Accept a fund order once per key; an order due at once settles in the same call.

    Raises AckLost, after the order and its effect are written, when `bank_ack_lost` fires.
    """
    request = find_request(uow, order.key)
    if request is not None:
        return request
    request = _insert_request(
        uow,
        order.key,
        BankRequestStatus.EXECUTED,
        {
            "operation_id": order.operation_id,
            "account_id": order.account_id,
            "product_id": order.product_id,
            "side": order.side.value,
            "shares": str(order.shares),
            "nav": str(order.nav),
            "gross_fen": order.gross_fen,
            "fee_fen": order.fee_fen,
            "cash_fen": order.cash_fen,
            "accepted_at": to_db(at),
            "settle_due_at": to_db(order.settle_due_at),
            "settled_at": None,
            "reason": None,
        },
    )
    if settle_now:
        request = settle_fund_order(uow, order.key, at)
    if faults.consume(uow, Fault.BANK_ACK_LOST):
        raise AckLost(request)
    return request


def settle_fund_order(uow: UnitOfWork, key: str, at: datetime) -> BankRequest:
    """Settle a fund order: cash to or from the customer, the fund side and the fee income.

    A settled or rejected order is returned unchanged, so settling twice changes nothing.
    """

    def postings(effect: Mapping[str, Any]) -> tuple[Posting, ...]:
        cash, fee = int(effect["cash_fen"]), int(effect["fee_fen"])
        entries = (
            Posting(str(effect["account_id"]), cash),
            Posting(FUND_SETTLEMENT_ACCOUNT, -cash - fee),
            Posting(FEE_INCOME_ACCOUNT, fee),
        )
        # A purchase without a fee posts nothing to fee income.
        return tuple(entry for entry in entries if entry.amount_fen != 0)

    return _settle(uow, key, at, postings)


def post_incoming_payment(
    uow: UnitOfWork,
    *,
    customer_id: str,
    account_id: str,
    amount_fen: int,
    payer_name: str,
    remark: str,
    at: datetime,
) -> OperationRow:
    """Credit a payment from outside the bank to a customer account (simulator only).

    The payer name and remark are untrusted text: they are stored as the bank received them
    and never read as an instruction.
    """
    if amount_fen <= 0:
        raise ValueError("an incoming payment is positive")
    key = f"incoming:{secrets.token_hex(8)}"
    operation = operations.insert_operation(
        uow,
        key=key,
        request_hash=sha256_hex(
            {"kind": INCOMING_PAYMENT, "account_id": account_id, "amount_fen": amount_fen}
        ),
        kind=INCOMING_PAYMENT,
        submitted_at=at,
        status=OperationStatus.SETTLED,
        customer_id=customer_id,
        account_id=account_id,
        amount_fen=amount_fen,
        settled_at=at,
    )
    operations.append_event(uow, operation.operation_id, OperationEventKind.SETTLED, "incoming")
    _insert_request(
        uow,
        key,
        BankRequestStatus.EXECUTED,
        {
            "operation_id": operation.operation_id,
            "account_id": account_id,
            "amount_fen": amount_fen,
            "payer_name": payer_name,
            "remark": remark,
            "settled_at": to_db(at),
        },
    )
    post_entries(
        uow,
        operation.operation_id,
        (Posting(account_id, amount_fen), Posting(CLEARING_ACCOUNT, -amount_fen)),
        at,
    )
    audit.record(
        uow,
        AuditActor.SYSTEM,
        "incoming_payment",
        customer_id=customer_id,
        ref=operation.operation_id,
    )
    return operation


def post_external_debit(
    uow: UnitOfWork,
    *,
    customer_id: str,
    account_id: str,
    amount_fen: int,
    merchant: str,
    at: datetime,
) -> OperationRow:
    """Debit a customer account for a payment the bank did not take through WealthGuard.

    The money has left already, so nothing can refuse it except a balance that cannot cover
    it. The merchant text is untrusted and stored as received.
    """
    if amount_fen <= 0:
        raise ValueError("an external debit is positive")
    balance = uow.execute(
        "SELECT balance_fen FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    if balance is None or balance["balance_fen"] < amount_fen:
        raise WgError(ErrorCode.INSUFFICIENT_FUNDS, {"account_id": account_id})
    key = f"external-debit:{secrets.token_hex(8)}"
    operation = operations.insert_operation(
        uow,
        key=key,
        request_hash=sha256_hex(
            {"kind": EXTERNAL_DEBIT, "account_id": account_id, "amount_fen": amount_fen}
        ),
        kind=EXTERNAL_DEBIT,
        submitted_at=at,
        status=OperationStatus.SETTLED,
        customer_id=customer_id,
        account_id=account_id,
        amount_fen=amount_fen,
        settled_at=at,
    )
    operations.append_event(uow, operation.operation_id, OperationEventKind.SETTLED, "external")
    _insert_request(
        uow,
        key,
        BankRequestStatus.EXECUTED,
        {
            "operation_id": operation.operation_id,
            "account_id": account_id,
            "amount_fen": amount_fen,
            "merchant": merchant,
            "settled_at": to_db(at),
        },
    )
    post_entries(
        uow,
        operation.operation_id,
        (Posting(account_id, -amount_fen), Posting(CLEARING_ACCOUNT, amount_fen)),
        at,
    )
    audit.record(
        uow,
        AuditActor.SYSTEM,
        "external_debit",
        customer_id=customer_id,
        ref=operation.operation_id,
    )
    return operation
