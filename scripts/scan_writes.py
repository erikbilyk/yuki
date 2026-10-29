#!/usr/bin/env python3
"""Fail when a SQL write statement appears in a string constant outside backend/wg/k6/."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "backend" / "wg"
ALLOWED = PACKAGE / "k6"
# Write statements anywhere in the string (after a CTE, another statement or a comment too).
# Upper-case keywords in statement shape, so prose such as "update the plan" does not match.
WRITE = re.compile(
    r"\b(INSERT\s+(OR\s+[A-Z]+\s+)?INTO|REPLACE\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM"
    r"|CREATE\s+(TEMP\s+|UNIQUE\s+)?(TABLE|INDEX|TRIGGER|VIEW)|DROP\s+(TABLE|INDEX|TRIGGER|VIEW)"
    r"|ALTER\s+TABLE)\b"
)


def find_hits(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and WRITE.search(node.value)
    ]


def main() -> int:
    hits = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.is_relative_to(ALLOWED):
            continue
        hits.extend(f"{path.relative_to(ROOT)}:{line}" for line in find_hits(path))
    for hit in hits:
        print(hit)
    if hits:
        print(f"write-path scan failed: {len(hits)} SQL write string(s) outside backend/wg/k6/")
        return 1
    print("write-path scan ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
