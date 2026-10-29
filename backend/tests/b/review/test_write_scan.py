"""The release scan in check.sh finds SQL writes only at the start of a string (SPEC §11).

`scripts/scan_writes.py` matches a write keyword at the start of a string constant. A write
after a CTE, after another statement or after a leading comment passes the scan unseen.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "scan_writes.py"

HIDDEN_WRITES = """
CTE = "WITH stale AS (SELECT lock_id FROM locks) DELETE FROM locks WHERE lock_id IN stale"
CHAINED = "SELECT 1; UPDATE approvals SET revoked_at = NULL"
COMMENTED = "-- clear the flag\\nUPDATE challenges SET consumed_at = NULL"
"""


def load_scan() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_writes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scan_finds_writes_that_do_not_start_the_string(tmp_path: Path) -> None:
    source = tmp_path / "hidden.py"
    source.write_text(HIDDEN_WRITES, encoding="utf-8")
    hits = load_scan().find_hits(source)
    assert hits == [2, 3, 4], f"scan_writes.py found only lines {hits} of 2, 3, 4"
