"""Draft transfers and their cards, created directly through K6 and K4.

The draft and request-approval routes belong to the gateway; these helpers stand in for
them so the authority layer can be tested on its own.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from wg.contracts.api import CardLine
from wg.contracts.enums import ActionKind
from wg.contracts.ids import IdPrefix, new_id
from wg.core import k3_policy, k4_approval
from wg.core.hashes import sha256_hex
from wg.core.k4_approval import CardContent, SimulatedSms, StepUp
from wg.k6 import actions
from wg.k6.actions import ActionRow
from wg.k6.approvals import CardRow
from wg.k6.clock import advance_clock
from wg.k6.db import Database
from wg.k6.simtime import now

MAIN = "cus_main"
OTHER = "cus_other"
MOM = "pay_mom"
WANGFANG = "pay_wangfang"
YELLOW_FEN = 50_000
RED_FEN = 150_000

STEP_UP = StepUp(code_key="test-code-key-" + "0" * 32, provider=SimulatedSms())


def draft_transfer(
    db: Database,
    amount_fen: int,
    payee_id: str = MOM,
    customer_id: str = MAIN,
    account_id: str = "acc_main",
) -> ActionRow:
    """A TRANSFER in DRAFT with the tier of the current day total."""
    params = {"account_id": account_id, "payee_id": payee_id, "amount_fen": amount_fen}

    def insert(uow):
        tier = k3_policy.transfer_tier(uow, customer_id, amount_fen, now(uow))
        return actions.insert_action(
            uow,
            logical_action_id=new_id(IdPrefix.ACTION),
            customer_id=customer_id,
            kind=ActionKind.TRANSFER,
            params=params,
            request_hash=sha256_hex({"kind": "TRANSFER", "customer_id": customer_id, **params}),
            tier=tier,
        )

    return db.run_in_uow(insert, "test draft")


def content(amount_fen: int) -> CardContent:
    line = CardLine(key="amount", label="金额", text=f"¥{amount_fen // 100}.{amount_fen % 100:02d}")
    return CardContent(title="转账", lines=(line,))


def open_card(db: Database, action: ActionRow) -> CardRow:
    return db.run_in_uow(
        lambda uow: k4_approval.open_action_card(
            uow, action, content(int(action.params["amount_fen"])), now(uow)
        ),
        "test card",
    )


def transfer_card(db: Database, amount_fen: int, **options) -> tuple[ActionRow, CardRow]:
    action = draft_transfer(db, amount_fen, **options)
    return action, open_card(db, action)


def advance(db: Database, delta: timedelta) -> datetime:
    def move(uow):
        state = advance_clock(uow, now(uow) + delta)
        k4_approval.expire_open_challenges(uow, STEP_UP, state.now)
        return state.now

    return db.run_in_uow(move, "test clock")


def clock(db: Database) -> datetime:
    with db.read() as conn:
        return now(conn)


def current(db: Database, logical_action_id: str) -> ActionRow:
    with db.read() as conn:
        action = actions.find_current(conn, logical_action_id)
    assert action is not None
    return action
