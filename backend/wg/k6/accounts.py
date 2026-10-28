"""Accounts and payees as stored (SPEC §7, §23.2). Balances change only through the ledger."""

from __future__ import annotations

from dataclasses import dataclass

from wg.contracts.enums import PayeeBank
from wg.k6.db import Reader


@dataclass(frozen=True)
class AccountRow:
    account_id: str
    customer_id: str | None
    name: str
    balance_fen: int
    covered: bool


@dataclass(frozen=True)
class PayeeRow:
    payee_id: str
    customer_id: str
    alias: str
    display_name: str
    bank: PayeeBank
    account_masked: str
    active: bool


def _account(row) -> AccountRow:
    return AccountRow(
        account_id=row["account_id"],
        customer_id=row["customer_id"],
        name=row["name"],
        balance_fen=row["balance_fen"],
        covered=bool(row["covered"]),
    )


def _payee(row) -> PayeeRow:
    return PayeeRow(
        payee_id=row["payee_id"],
        customer_id=row["customer_id"],
        alias=row["alias"],
        display_name=row["display_name"],
        bank=PayeeBank(row["bank"]),
        account_masked=row["account_masked"],
        active=row["status"] == "active",
    )


def find_account(reader: Reader, account_id: str) -> AccountRow | None:
    row = reader.execute(
        "SELECT * FROM accounts WHERE account_id = ? AND kind = 'customer'", (account_id,)
    ).fetchone()
    return None if row is None else _account(row)


def customer_accounts(reader: Reader, customer_id: str) -> list[AccountRow]:
    """The customer's accounts in profile order; the first one is the main account."""
    rows = reader.execute(
        "SELECT * FROM accounts WHERE customer_id = ? AND kind = 'customer' ORDER BY rowid",
        (customer_id,),
    ).fetchall()
    return [_account(row) for row in rows]


def main_account(reader: Reader, customer_id: str) -> AccountRow | None:
    accounts = customer_accounts(reader, customer_id)
    return accounts[0] if accounts else None


def find_payee(reader: Reader, payee_id: str) -> PayeeRow | None:
    row = reader.execute("SELECT * FROM payees WHERE payee_id = ?", (payee_id,)).fetchone()
    return None if row is None else _payee(row)


def customer_payees(reader: Reader, customer_id: str, query: str | None = None) -> list[PayeeRow]:
    """Active payees of the customer whose alias or name contains `query`, in book order."""
    rows = reader.execute(
        "SELECT * FROM payees WHERE customer_id = ? AND status = 'active' ORDER BY rowid",
        (customer_id,),
    ).fetchall()
    payees = [_payee(row) for row in rows]
    if query:
        payees = [p for p in payees if query in p.alias or query in p.display_name]
    return payees
