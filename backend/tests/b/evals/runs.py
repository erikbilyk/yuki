"""Run directories for the scorer tests: real database copies and hand-written episode files."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from ..acceptance.plan_flow import at_day, bought, redeemed, redemption_settled, rent_paid
from ..harness.client import WgClient


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
