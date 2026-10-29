"""The fixed list of transaction categories (SPEC §7, S2 §7).

A category changes cash figures only where it says so: a transfer between the customer's own
accounts and a merchant refund are neither income nor spending. Statement lines carry a
category hint; a customer's resolution sets `category_id` from this list.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from wg.k6.db import UnitOfWork
from wg.k6.simtime import now_db

OWN_TRANSFER = "own_transfer"
REFUND = "refund"

# Incoming lines in these categories are not income.
NOT_INCOME = frozenset({OWN_TRANSFER, REFUND})
# Lines in these categories are not everyday spending.
NOT_SPENDING = frozenset({OWN_TRANSFER, REFUND})

CATEGORIES: Mapping[str, str] = MappingProxyType(
    {
        "salary": "工资",
        "rent": "房租",
        "utilities": "水电燃气",
        "groceries": "超市购物",
        "dining": "餐饮",
        "transport": "交通出行",
        "shopping": "购物",
        "family_transfer": "家人转账",
        "insurance": "保险",
        "personal_transfer": "个人转账",
        "debt_repayment": "归还借款",
        OWN_TRANSFER: "本人账户互转",
        REFUND: "退款",
    }
)


def seed_categories(uow: UnitOfWork) -> None:
    uow.executemany(
        "INSERT INTO categories (category_id, label, created_at) VALUES (?, ?, ?)",
        [(code, label, now_db(uow)) for code, label in CATEGORIES.items()],
    )
