"""Dated public diagnostics: import checks and the read route (SPEC §22, T41 support)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from wg.k6.calc_results import PUBLIC_CUSTOMER_ID, CalcKind
from wg.services.public_import import PUBLIC_DIR, PublicImportError, build_output, import_public


@pytest.fixture
def public_dir(tmp_path: Path) -> Path:
    target = tmp_path / "public"
    shutil.copytree(PUBLIC_DIR, target, ignore=shutil.ignore_patterns("raw"))
    return target


def _import(browser, public_dir: Path):
    return browser.db.run_in_uow(lambda uow: import_public(uow, public_dir), "public import")


def test_route_refuses_until_imported(browser) -> None:
    response = browser.get("/v1/funds/public-diagnostics")
    assert response.status_code == 409
    assert response.json()["code"] == "DATA_INCOMPLETE"


def test_import_serves_dated_overlap_and_fees(browser, public_dir: Path) -> None:
    stored = _import(browser, public_dir)
    body = browser.get("/v1/funds/public-diagnostics").json()
    assert body["calc_result_id"] == stored.calc_result_id
    assert body["period_end"] == "2026-06-30"
    assert body["executable"] is False
    assert body["overlap"]["overlap_percent"] == "26.709382"
    assert body["overlap"]["shared_securities"] == 61
    assert {fund["product_id"] for fund in body["funds"]} == {"010737", "019018"}
    for fund in body["funds"]:
        assert [t["rate"] for t in fund["redemption_tiers"]] == ["0.0150", "0.0050", "0.0000"]
        assert fund["source_url"] is None


def test_import_is_idempotent_and_owned_by_the_system_customer(browser, public_dir: Path) -> None:
    first = _import(browser, public_dir)
    second = _import(browser, public_dir)
    assert first.calc_result_id == second.calc_result_id
    with browser.db.read() as conn:
        rows = conn.execute(
            "SELECT customer_id, kind FROM calc_results WHERE kind = ?",
            (CalcKind.PUBLIC_DIAGNOSTICS.value,),
        ).fetchall()
        holdings = conn.execute("SELECT COUNT(*) FROM holdings_snapshots WHERE executable = 0")
        assert holdings.fetchone()[0] == 0
    assert [tuple(r) for r in rows] == [(PUBLIC_CUSTOMER_ID, "public_diagnostics")]


def test_system_customer_cannot_log_in(browser, public_dir: Path) -> None:
    _import(browser, public_dir)
    response = browser.client.post(
        "/v1/auth/login", json={"handle": "system-public", "password": "!"}
    )
    assert response.status_code == 401


def test_import_refuses_failed_reconciliation(public_dir: Path) -> None:
    path = public_dir / "reconciliation.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"]["019018"][0]["ok"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PublicImportError, match="reconciliation"):
        build_output(public_dir)


def test_import_refuses_altered_holdings(public_dir: Path) -> None:
    path = public_dir / "holdings_010737.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if line.split(",")[4] == "300308")
    fields = lines[index].split(",")
    fields[7] = str(int(fields[7]) // 2)
    lines[index] = ",".join(fields)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(PublicImportError, match="overlap recomputed"):
        build_output(public_dir)


def test_import_refuses_report_hash_mismatch(public_dir: Path) -> None:
    path = public_dir / "reports.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("e590d339", "00000000", 1), encoding="utf-8")
    with pytest.raises(PublicImportError, match="another report"):
        build_output(public_dir)
