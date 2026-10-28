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
WRITE = re.compile(r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", re.IGNORECASE)


def find_hits(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and WRITE.match(node.value)
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
