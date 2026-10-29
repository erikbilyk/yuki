"""The gateway bench with the coordinator and the K8 failure policy; plans built through K6.

A plan with several nodes is composed here from K6 repositories and approved with a
plan-scope approval written directly, because plan composition and plan cards are routes
of their own (A09, A10). Process exits are exceptions, as on the gateway bench.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from wg.contracts.enums import ActionKind, ApprovalScopeKind, PlanKind, Tier
from wg.contracts.ids import IdPrefix, new_id
from wg.core import k8_audit as k8
from wg.core.hashes import sha256_hex
from wg.core.k5_coordinator import Coordinator
from wg.k6 import actions, approvals, fixture, plans
from wg.k6.db import Database
from wg.k6.gateway import Gateway
from wg.k6.migrate import migrate
from wg.k6.simtime import SHANGHAI, now
from wg.packs.registry import pack_for

from ..core.drafts import MAIN, STEP_UP
from ..gateway.conftest import Bench, Exited, Paused

EPOCH = datetime(2026, 11, 2, 9, 0, tzinfo=SHANGHAI)
MOM = "pay_mom"
DAD = "pay_dad"


@dataclass(frozen=True)
class Node:
    name: str
    amount_fen: int
    payee_id: str = MOM
    tier: Tier = Tier.YELLOW
    depends_on: Sequence[str] = ()
    not_before: datetime | None = None


@dataclass(kw_only=True)
class Desk(Bench):
    coordinator: Coordinator

    def plan(self, nodes: Sequence[Node], expires_at: datetime | None = None) -> Composed:
        """A mixed plan of TRANSFER nodes with a plan-scope approval valid until `expires_at`."""
        pack = pack_for(ActionKind.TRANSFER)
        ids = {node.name: new_id(IdPrefix.ACTION) for node in nodes}

        def compose(uow):
            plan = plans.insert_plan(uow, MAIN, PlanKind.MIXED)
            for node in nodes:
                params = pack.canonical(
                    ActionKind.TRANSFER,
                    {
                        "account_id": "acc_main",
                        "payee_id": node.payee_id,
                        "amount_fen": node.amount_fen,
                    },
                )
                actions.insert_action(
                    uow,
                    logical_action_id=ids[node.name],
                    customer_id=MAIN,
                    kind=ActionKind.TRANSFER,
                    params=params,
                    request_hash=pack.request_hash(ActionKind.TRANSFER, MAIN, params),
                    tier=node.tier,
                    plan_id=plan.plan_id,
                    depends_on=[ids[d] for d in node.depends_on],
                    not_before=node.not_before,
                )
            terms = {"plan_id": plan.plan_id, "nodes": sorted(ids.values())}
            approval = approvals.insert_approval(
                uow,
                approval_id=new_id(IdPrefix.APPROVAL),
                customer_id=MAIN,
                scope_kind=ApprovalScopeKind.PLAN,
                terms=terms,
                terms_hash=sha256_hex(terms),
                expires_at=expires_at or now(uow) + timedelta(days=9),
                plan_id=plan.plan_id,
            )
            plans.set_root_approval(uow, plan, approval.approval_id)
            return plan.plan_id

        return Composed(self.db.run_in_uow(compose, "test compose"), ids)

    def status(self, logical_action_id: str):
        return self.current(logical_action_id).status

    def plan_status(self, plan_id: str | None):
        assert plan_id is not None
        with self.db.read() as conn:
            return plans.get_plan(conn, plan_id).status

    def count(self, table: str) -> int:
        return self.rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]

    def authority(self) -> dict[str, int]:
        return {
            "approvals": self.count("approvals"),
            "challenges": self.count("challenges"),
            "grants": self.rows(
                "SELECT COUNT(*) AS n FROM challenges WHERE verified_at IS NOT NULL"
            )[0]["n"],
        }

    def tamper(self, sql: str, *params) -> None:
        """A write that bypasses the product, as a broken disk or a hostile operator would."""
        conn = sqlite3.connect(self.db.path)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


@dataclass(frozen=True)
class Composed:
    plan_id: str
    ids: dict[str, str]

    def __getitem__(self, name: str) -> str:
        return self.ids[name]


@pytest.fixture
def desk(tmp_path: Path) -> Iterator[Desk]:
    db = Database(tmp_path / "wg.db")
    migrate(db)
    db.failure_policy = k8.on_failure
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
    coordinator = Coordinator(db, gateway, STEP_UP, exit_process=exit_process)
    yield Desk(db=db, gateway=gateway, hooks=hooks, coordinator=coordinator)
    db.close()
