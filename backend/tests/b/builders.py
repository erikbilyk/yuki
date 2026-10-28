"""Builders that turn plain numbers or the fixture into cash engine inputs."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from wg.contracts.engines import (
    Assumption,
    CashAccountInput,
    CashInputs,
    CommitmentInput,
    HoldInput,
    UnresolvedInput,
)
from wg.contracts.enums import CommitmentStatus

from .goldens.data import load_fixture

EPOCH = datetime.fromisoformat("2026-11-02T09:00:00+08:00")


def account(
    posted_fen: int, account_id: str = "acc_main", *, covered: bool = True, balanced: bool = True
) -> CashAccountInput:
    return CashAccountInput(
        account_id=account_id,
        posted_fen=posted_fen,
        covered=covered,
        opening_fen=0,
        imported_sum_fen=posted_fen if balanced else posted_fen - 100,
        closing_fen=posted_fen,
    )


def bill(
    name: str,
    unpaid_fen: int,
    due: date,
    *,
    status: CommitmentStatus = CommitmentStatus.CONFIRMED,
    amount_fen: int | None = None,
) -> CommitmentInput:
    return CommitmentInput(
        commitment_id=f"cmt_{name}",
        purpose_id=f"pur_{name}",
        status=status,
        amount_fen=unpaid_fen if amount_fen is None else amount_fen,
        unpaid_fen=unpaid_fen,
        due_date=due,
        payee_id=f"pay_{name}",
    )


def hold(operation: str, amount_fen: int, purpose: str | None = None) -> HoldInput:
    return HoldInput(
        operation_id=f"opn_{operation}",
        purpose_id=None if purpose is None else f"pur_{purpose}",
        amount_fen=amount_fen,
    )


def inputs(
    *,
    posted_fen: int = 1_800_000,
    reserve_fen: int = 400_000,
    allowance_fen: int = 300_000,
    margin_fen: int = 60_000,
    bills: tuple[CommitmentInput, ...] = (),
    holds: tuple[HoldInput, ...] = (),
    accounts: tuple[CashAccountInput, ...] | None = None,
    unresolved: tuple[UnresolvedInput, ...] = (),
    income_fen: int = 1_200_000,
    spent_fen: int = 60_000,
    as_of: datetime = EPOCH,
    horizon_days: int = 30,
    assumptions: tuple[Assumption, ...] = (),
) -> CashInputs:
    return CashInputs(
        as_of=as_of,
        rules_version="rules_v1",
        accounts=(account(posted_fen),) if accounts is None else accounts,
        reserve_fen=reserve_fen,
        allowance_remaining_fen=allowance_fen,
        margin_fen=margin_fen,
        commitments=bills,
        holds=holds,
        unresolved=unresolved,
        income_this_month_fen=income_fen,
        spent_this_month_fen=spent_fen,
        horizon_days=horizon_days,
        assumptions=assumptions,
    )


def fixture_inputs(variant: str | None = None, **changes: Any) -> CashInputs:
    """Cash inputs of the main customer at the epoch, read from the fixture file.

    Each account reconciles over its whole statement chain: the first opening balance plus
    every imported transaction against the last closing balance.
    """
    doc = load_fixture(variant)
    epoch = datetime.fromisoformat(doc["epoch"])
    accounts = []
    for acc in doc["accounts"]:
        if acc["customer"] != "cus_main":
            continue
        chain = [s for s in doc["statements"] if s["account"] == acc["id"]]
        accounts.append(
            CashAccountInput(
                account_id=acc["id"],
                posted_fen=acc["opening_fen"],
                covered=acc["covered"],
                opening_fen=chain[0]["opening_fen"],
                imported_sum_fen=sum(t["amount_fen"] for s in chain for t in s["transactions"]),
                closing_fen=chain[-1]["closing_fen"],
            )
        )
    commitments = tuple(
        CommitmentInput(
            commitment_id=c["id"],
            purpose_id=c["purpose_id"],
            status=CommitmentStatus(c["status"]),
            amount_fen=c["amount_fen"],
            unpaid_fen=c["unpaid_fen"],
            due_date=(epoch + timedelta(days=c["due_offset_days"])).date(),
            payee_id=c["payee"],
        )
        for c in doc["commitments"]
        if c["customer"] == "cus_main"
    )
    base = inputs(
        accounts=tuple(accounts),
        reserve_fen=doc["reserve"]["target_fen"],
        allowance_fen=doc["allowance"]["remaining_fen"],
        margin_fen=doc["margin_fen"],
        bills=commitments,
        income_fen=doc["income_this_month_fen"],
        spent_fen=doc["spent_this_month_fen"],
        as_of=epoch,
    )
    return base.model_copy(update=changes)
