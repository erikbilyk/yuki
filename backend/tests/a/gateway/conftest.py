"""A database at the epoch of the base profile with the gateway; drafts come from the pack.

Process-ending faults are replaced by exceptions that are not `Exception`, so they unwind
the unit of work exactly as a process exit would leave it: uncommitted.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from wg.contracts.enums import ActionKind, ActionStatus, Fault
from wg.contracts.ids import IdPrefix, new_id
from wg.core import k4_approval as k4
from wg.k6 import actions, challenges, faults, fixture
from wg.k6.actions import ActionRow
from wg.k6.clock import advance_clock
from wg.k6.db import Database
from wg.k6.gateway import Gateway, Submission
from wg.k6.migrate import migrate
from wg.k6.simtime import now
from wg.packs.registry import pack_for

from ..core.drafts import MAIN, STEP_UP

MOM = "pay_mom"
DAD = "pay_dad"
START_FEN = 1_800_000


class Exited(BaseException):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class Paused(BaseException):
    def __init__(self, marker: Path) -> None:
        super().__init__(str(marker))
        self.marker = marker


@dataclass
class Bench:
    db: Database
    gateway: Gateway
    hooks: list[str] = field(default_factory=list)

    def draft(self, amount_fen: int, payee_id: str = MOM) -> ActionRow:
        """A TRANSFER from the main account in DRAFT, with the tier of the current day total."""
        kind = ActionKind.TRANSFER
        pack = pack_for(kind)
        params = pack.canonical(
            kind, {"account_id": "acc_main", "payee_id": payee_id, "amount_fen": amount_fen}
        )

        def insert(uow):
            return actions.insert_action(
                uow,
                logical_action_id=new_id(IdPrefix.ACTION),
                customer_id=MAIN,
                kind=kind,
                params=params,
                request_hash=pack.request_hash(kind, MAIN, params),
                tier=pack.tier(uow, MAIN, kind, params, now(uow)),
            )

        return self.db.run_in_uow(insert, "test draft")

    def confirmed(self, amount_fen: int, payee_id: str = MOM) -> ActionRow:
        """Drafted, shown on a card and confirmed: READY when YELLOW, AWAITING_STEPUP when RED."""
        return self.confirm_draft(self.draft(amount_fen, payee_id))

    def confirm_draft(self, draft: ActionRow) -> ActionRow:
        """Show the draft on its card and confirm it with the card's terms hash."""
        pack = pack_for(draft.kind)

        def confirm(uow):
            at = now(uow)
            action = actions.get_revision(uow, draft.logical_action_id, draft.revision)
            card = k4.open_action_card(uow, action, pack.card(uow, action, at), at)
            return k4.confirm_card(
                uow,
                STEP_UP,
                customer_id=MAIN,
                card_id=card.card_id,
                presented_terms_hash=card.terms_hash,
                now=at,
            )

        return self.db.run_in_uow(confirm, "test confirm").action

    def ready(self, amount_fen: int, payee_id: str = MOM) -> ActionRow:
        """A confirmed action that may be submitted; a RED one is verified with its code first."""
        return self.ready_from(self.draft(amount_fen, payee_id))

    def ready_from(self, draft: ActionRow) -> ActionRow:
        action = self.confirm_draft(draft)
        if action.status is ActionStatus.AWAITING_STEPUP:
            action = self.verify(action)
        assert action.status is ActionStatus.READY
        return action

    def verify(self, action: ActionRow) -> ActionRow:
        with self.db.read() as conn:
            issued = challenges.challenges_of_action(
                conn, action.logical_action_id, action.revision
            )
            challenge = next(c for c in issued if c.is_open)
            code = conn.execute(
                "SELECT code FROM sms_outbox WHERE challenge_id = ?", (challenge.challenge_id,)
            ).fetchone()[0]
        outcome = self.db.run_in_uow(
            lambda uow: k4.verify_code(
                uow,
                STEP_UP,
                customer_id=MAIN,
                challenge_id=challenge.challenge_id,
                code=code,
                now=now(uow),
            ),
            "test verify",
        )
        assert isinstance(outcome, k4.Verified), outcome
        return outcome.action

    def submit(self, action: ActionRow) -> Submission:
        return self.gateway.submit(MAIN, action.logical_action_id)

    def current(self, logical_action_id: str) -> ActionRow:
        with self.db.read() as conn:
            action = actions.find_current(conn, logical_action_id)
        assert action is not None
        return action

    def arm(self, fault: Fault, count: int = 1) -> None:
        self.db.run_in_uow(lambda uow: faults.arm(uow, fault, count), "test arm")

    def armed(self) -> set[Fault]:
        with self.db.read() as conn:
            return {f.fault for f in faults.armed_faults(conn)}

    def advance_to(self, target: datetime) -> None:
        self.db.run_in_uow(lambda uow: advance_clock(uow, target), "test clock")

    def advance(self, delta: timedelta) -> datetime:
        with self.db.read() as conn:
            target = now(conn) + delta
        self.advance_to(target)
        return target

    def rows(self, sql: str, *params) -> list[dict]:
        with self.db.read() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def balance(self, account_id: str = "acc_main") -> int:
        return self.rows("SELECT balance_fen FROM accounts WHERE account_id = ?", account_id)[0][
            "balance_fen"
        ]


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    db = Database(tmp_path / "wg.db")
    migrate(db)
    fixture.load_into_empty(db, fixture.read_profile(fixture.DEFAULT_PROFILE))
    hooks: list[str] = []

    def exit_process(status: int) -> None:
        hooks.append(f"exit:{status}")
        raise Exited(status)

    def pause(marker: Path) -> None:
        hooks.append("pause")
        raise Paused(marker)

    gateway = Gateway(
        db, fault_wait_file=tmp_path / "fault.wait", exit_process=exit_process, pause=pause
    )
    yield Bench(db, gateway, hooks)
    db.close()
