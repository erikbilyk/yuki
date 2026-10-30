"""python -m evals.score RUN_DIR: write score.json and score.md into the run directory.

Exit status: 0 when scoring finished without unauthorized or duplicate effects, 1 when it found
some, 2 when the run directory cannot be read.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .report import write
from .scorer import score_run
from .source import ScoreInputError, load_run


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m evals.score RUN_DIR", file=sys.stderr)
        return 2
    try:
        run = load_run(Path(argv[0]))
    except ScoreInputError as error:
        print(f"score: {error}", file=sys.stderr)
        return 2
    doc = write(run, score_run(run))
    totals = doc["totals"]
    print(
        f"score {doc['run_id']}: {totals['passed']}/{totals['episodes']} passed, "
        f"{totals['unauthorized_effects']} unauthorized, {totals['duplicate_effects']} duplicate "
        f"effects; wrote {run.path / 'score.md'}"
    )
    return 1 if totals["unauthorized_effects"] or totals["duplicate_effects"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
