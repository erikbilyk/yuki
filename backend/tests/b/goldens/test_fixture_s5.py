"""fixture_s5 equals fixture_v1 plus the merchants, agreements and variants of S5-SPEC §10.

Both files are read as plain YAML; no application code is imported.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from .data import FIXTURE_PATH, apply_variant, by_id, date_at, load_raw_fixture

S5_PATH = FIXTURE_PATH.with_name("fixture_s5.yaml")
S5_ONLY_KEYS = {"merchants", "mandates"}
PER_PROFILE_KEYS = {"profile", "variants"}

MERCHANTS = {
    "mer_video": ("模拟视频会员", ["视频", "视频会员", "影视会员"], "SIM-MER-0001", True),
    "mer_cloud": ("模拟云盘", ["云盘", "云存储", "网盘"], "SIM-MER-0002", True),
    "mer_fitness": ("模拟健身会所", ["健身", "健身房", "健身会所", "健身卡"], "SIM-MER-0003", True),
    "mer_music": ("模拟音乐会员", ["音乐", "音乐会员"], "SIM-MER-0004", True),
    "mer_unverified": ("未核验商户", [], "SIM-MER-0099", False),
}
# id: (customer, account, merchant, purpose_id, expected_fen, cap_fen, debit_day)
MANDATES = {
    "mdt_video": ("cus_main", "acc_main", "mer_video", "pur_sub_video", 2500, 3000, 5),
    "mdt_cloud": ("cus_main", "acc_main", "mer_cloud", "pur_sub_cloud", 2100, 2100, 12),
    "mdt_fitness": ("cus_main", "acc_main", "mer_fitness", "pur_sub_fitness", 19900, 22000, 20),
    "mdt_other_video": (
        "cus_other",
        "acc_other",
        "mer_video",
        "pur_sub_other_video",
        2500,
        3000,
        8,
    ),
}


def read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="module")
def base() -> dict[str, Any]:
    return load_raw_fixture()


@pytest.fixture(scope="module")
def s5() -> dict[str, Any]:
    return read(S5_PATH)


def test_shared_keys_are_identical(base: dict[str, Any], s5: dict[str, Any]) -> None:
    shared = set(base) - PER_PROFILE_KEYS
    assert set(s5) == shared | S5_ONLY_KEYS | PER_PROFILE_KEYS
    different = sorted(key for key in shared if base[key] != s5[key])
    assert not different, f"keys that differ from fixture_v1: {different}"


def test_profile_name(s5: dict[str, Any]) -> None:
    assert s5["profile"] == "fixture_s5"


def test_merchants_match_the_table(s5: dict[str, Any]) -> None:
    rows = by_id(s5["merchants"])
    assert list(rows) == list(MERCHANTS)
    for merchant_id, (name, aliases, ref, verified) in MERCHANTS.items():
        row = rows[merchant_id]
        assert row == {
            "id": merchant_id,
            "display_name": name,
            "aliases": aliases,
            "settlement_account_ref": ref,
            "verified": verified,
        }


def test_mandates_match_the_table(s5: dict[str, Any]) -> None:
    rows = by_id(s5["mandates"])
    assert list(rows) == list(MANDATES)
    for mandate_id, (customer, account, merchant, purpose, expected, cap, day) in MANDATES.items():
        assert rows[mandate_id] == {
            "id": mandate_id,
            "customer": customer,
            "account": account,
            "merchant": merchant,
            "purpose_id": purpose,
            "expected_fen": expected,
            "cap_fen": cap,
            "debit_day": day,
            "starts_offset_days": -31,
            "expires_offset_days": 333,
            "status": "ACTIVE",
        }


def test_mandate_references_and_dates(s5: dict[str, Any]) -> None:
    accounts = by_id(s5["accounts"])
    merchants = by_id(s5["merchants"])
    customers = by_id(s5["customers"])
    for mandate in s5["mandates"]:
        assert mandate["customer"] in customers
        assert accounts[mandate["account"]]["customer"] == mandate["customer"]
        assert merchants[mandate["merchant"]]["verified"]
        assert mandate["cap_fen"] >= mandate["expected_fen"] >= 1
        assert 1 <= mandate["debit_day"] <= 31
        starts = date_at(s5, mandate["starts_offset_days"])
        expires = date_at(s5, mandate["expires_offset_days"])
        assert starts == date(2026, 10, 2) and expires == date(2027, 10, 1)
    purposes = [m["purpose_id"] for m in s5["mandates"]]
    assert len(purposes) == len(set(purposes))


def test_the_variants_are_the_two_of_the_spec(s5: dict[str, Any]) -> None:
    assert s5["variants"] == {
        "video_suspended": {"set": {"mandates.mdt_video.status": "SUSPENDED"}},
        "video_expiring": {"set": {"mandates.mdt_video.expires_offset_days": 3}},
    }
    suspended = by_id(apply_variant(s5, "video_suspended")["mandates"])
    assert suspended["mdt_video"]["status"] == "SUSPENDED"
    assert suspended["mdt_cloud"]["status"] == "ACTIVE"
    expiring = apply_variant(s5, "video_expiring")
    # The last day of authority is the epoch plus three days: 2026-11-05.
    assert date_at(expiring, by_id(expiring["mandates"])["mdt_video"]["expires_offset_days"]) == (
        date(2026, 11, 5)
    )


def test_fixture_v1_has_no_agreements(base: dict[str, Any]) -> None:
    assert S5_ONLY_KEYS.isdisjoint(base)
