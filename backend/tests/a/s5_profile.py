"""A direct-debit profile for tests: fixture_v1 with the merchants and agreements (S5-SPEC §10).

Written to a temporary file by the tests, so they do not depend on fixture_s5.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

BASE_PROFILE = Path(__file__).resolve().parents[1] / "fixtures" / "fixture_v1.yaml"

MERCHANTS = [
    ("mer_video", "模拟视频会员", ["视频", "视频会员", "影视会员"], "SIM-MER-0001", True),
    ("mer_cloud", "模拟云盘", ["云盘", "云存储", "网盘"], "SIM-MER-0002", True),
    ("mer_fitness", "模拟健身会所", ["健身", "健身房", "健身会所", "健身卡"], "SIM-MER-0003", True),
    ("mer_music", "模拟音乐会员", ["音乐", "音乐会员"], "SIM-MER-0004", True),
    ("mer_unverified", "未核验商户", [], "SIM-MER-0099", False),
]

MANDATES = [
    ("mdt_video", "cus_main", "acc_main", "mer_video", "pur_sub_video", 2500, 3000, 5),
    ("mdt_cloud", "cus_main", "acc_main", "mer_cloud", "pur_sub_cloud", 2100, 2100, 12),
    ("mdt_fitness", "cus_main", "acc_main", "mer_fitness", "pur_sub_fitness", 19900, 22000, 20),
    (
        "mdt_other_video",
        "cus_other",
        "acc_other",
        "mer_video",
        "pur_sub_other_video",
        2500,
        3000,
        8,
    ),
]


def s5_doc() -> dict[str, Any]:
    doc = yaml.safe_load(BASE_PROFILE.read_text(encoding="utf-8"))
    doc["profile"] = "fixture_s5"
    doc["merchants"] = [
        {
            "id": merchant_id,
            "display_name": name,
            "aliases": aliases,
            "settlement_account_ref": ref,
            "verified": verified,
        }
        for merchant_id, name, aliases, ref, verified in MERCHANTS
    ]
    doc["mandates"] = [
        {
            "id": ref,
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
        for ref, customer, account, merchant, purpose, expected, cap, day in MANDATES
    ]
    doc["variants"] = {
        "video_suspended": {"set": {"mandates.mdt_video.status": "SUSPENDED"}},
        "video_expiring": {"set": {"mandates.mdt_video.expires_offset_days": 3}},
    }
    return doc


def write_profile(directory: Path, doc: dict[str, Any] | None = None) -> Path:
    path = directory / "fixture_s5.yaml"
    path.write_text(yaml.safe_dump(doc or s5_doc(), allow_unicode=True), encoding="utf-8")
    return path
