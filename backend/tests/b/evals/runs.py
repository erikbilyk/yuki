"""Run directories for the scorer tests: real database copies and hand-written episode files."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from wg.contracts.enums import ActionKind

from ..acceptance.plan_flow import (
    INSURANCE,
    REVISED_FEN,
    SEARCH,
    S6Plan,
    approved,
    at_day,
    bought,
    confirm_new_bill,
    redeemed,
    redemption_settled,
    rent_paid,
    settled_at_t3,
)
from ..harness.client import WgClient
from ..harness.inject import set_settle_days


def copy_database(client: WgClient, target: Path) -> Path:
    """A consistent copy of the backend's database, as the runner leaves it after a stop."""
    source = sqlite3.connect(f"file:{client.db.path}?mode=ro", uri=True)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def tamper(database: Path, *statements: str) -> Path:
    """Run SQL on a database copy, past the triggers that keep the live database honest."""
    with closing(sqlite3.connect(database)) as db:
        for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall():
            db.execute(f"DROP TRIGGER {name}")
        for statement in statements:
            db.execute(statement)
        db.commit()
        db.execute("PRAGMA journal_mode = DELETE")
    return database


def base_path(client: WgClient) -> None:
    """The six-action plan from T+0 to T+7, every code entered on the due date."""
    plan = redeemed(client)
    rent_paid(client, plan)
    redemption_settled(client, plan)
    bought(client, plan)
    at_day(client, 5)
    at_day(client, 7)
    plan.pay_insurance()


def restarted_path(client: WgClient) -> None:
    """Redeemed at T+0; the rent code and the purchase code are both entered after T+3."""
    plan = redeemed(client)
    at_day(client, 3)
    plan.pay_rent()
    plan.buy()


def revised_purchase_path(client: WgClient) -> None:
    """The 1,200 yuan bill at T+3: the purchase is revised, then the base path to T+7."""
    plan = settled_at_t3(client)
    confirm_new_bill(client, 120_000)
    bought(client, plan, REVISED_FEN, revision=2)
    at_day(client, 5)
    at_day(client, 7)
    plan.pay_insurance()


def paused_purchase_path(client: WgClient) -> None:
    """The 1,600 yuan bill at T+3 pauses the purchase; rent and insurance still go through."""
    plan = settled_at_t3(client)
    confirm_new_bill(client, 160_000)
    at_day(client, 5)
    at_day(client, 7)
    plan.pay_insurance()


def bill_before_purchase_path(client: WgClient) -> None:
    """Rent at T+2, the 1,200 yuan bill at T+3, the revised purchase; no checkpoint yet."""
    plan = settled_at_t3(client)
    confirm_new_bill(client, 120_000)
    bought(client, plan, REVISED_FEN, revision=2)


def fund_plan_path(client: WgClient) -> None:
    """Only the fund plan: approved and redeemed at T+0, purchase code entered at T+3."""
    found = client.candidates(**SEARCH)
    plan = client.draft_plan(found.calc_result_id, True)
    view = client.plan(plan.plan_id)
    client.confirm(client.card(view.card_id))
    ids = {node.kind: node.logical_action_id for node in view.nodes}
    client.verify(client.challenge(ids[ActionKind.REDEEM]).challenge_id)
    at_day(client, 3)
    client.verify(client.challenge(ids[ActionKind.PURCHASE]).challenge_id)


def insurance_paid_path(client: WgClient) -> None:
    """The insurance paid at T+0 with its own code."""
    client.approve(client.draft_pay_bill(INSURANCE))


def late_redemption_path(client: WgClient) -> S6Plan:
    """The redemption slows to six days after the plan is approved; nothing is bought."""
    plan = approved(client)
    set_settle_days(client.db.path, "redeem_settle_days", 6)
    plan.redeem()
    rent_paid(client, plan)
    at_day(client, 5)
    at_day(client, 6)
    at_day(client, 7)
    plan.pay_insurance()
    return plan


def turn(
    index: int, text: str, reply: str = "", *, calls: int = 2, events: int = 0
) -> dict[str, Any]:
    return {
        "index": index,
        "customer_text": text,
        "http_status": 200,
        "reply": {"fact_lines": [], "explanation": reply},
        "disposition": None,
        "latency_ms": 100,
        "model": {"calls": calls, "cost_cny": "0.010000"},
        "tool_events": [{"event": "draft_created"} for _ in range(events)],
    }


def episode_record(
    scenario_id: str,
    *,
    database: str | None,
    expectations: dict[str, Any],
    system: str = "coordinated",
    repeat: int = 1,
    category: str = "colloquial",
    status: str = "completed",
    turns: list[dict[str, Any]] | None = None,
    dispositions: list[str | None] | None = None,
    cards: list[dict[str, Any]] | None = None,
    codes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    turns = turns if turns is not None else [turn(0, "给妈妈转500")]
    return {
        "schema_version": 1,
        "episode_id": f"{scenario_id}.{system}.r{repeat}",
        "scenario_id": scenario_id,
        "case": None,
        "category": category,
        "system": system,
        "repeat": repeat,
        "customer_id": "cus_main",
        "expectations": expectations,
        "status": status,
        "errors": [],
        "steps": [],
        "turns": turns,
        "dispositions": dispositions if dispositions is not None else [None] * len(turns),
        "cards": cards or [],
        "codes": codes or [],
        "k7_replacements": [],
        "model": {"calls": sum(t["model"]["calls"] for t in turns), "cost_cny": "0.020000"},
        "files": {"database": database},
    }


def write_run(root: Path, episodes: list[tuple[dict[str, Any], Path | None]]) -> Path:
    """`root/episodes/<id>.json` with the database copy beside it, and a manifest."""
    folder = root / "episodes"
    folder.mkdir(parents=True)
    for record, database in episodes:
        name = None
        if database is not None:
            name = f"{record['episode_id']}.db"
            (folder / name).write_bytes(database.read_bytes())
        record["files"] = {"database": name}
        (folder / f"{record['episode_id']}.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )
    manifest = {"run_id": root.name, "mode": "live", "build": {"commit": "abc", "dirty": False}}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root
