"""Run the acceptance cases and write one report: `python -m tests.b.harness.run [options]`.

Each repeat is a separate pytest session in a subprocess, so no state carries over between
repeats. Exit status 1 means a required case failed (with --gate: or has no passing test at
a required layer), or pytest could not run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from .catalogue import HARNESS_LAYERS, SIDE_GATES, load_catalogue
from .env import BACKEND_DIR
from .plugin import split_option
from .report import REPORTS_DIR, Selection, build_report, exit_code, write_report

ACCEPTANCE_DIR = "tests/b/acceptance"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="acceptance", description=__doc__)
    parser.add_argument("--gate", choices=["G1", "G2", "G3", *SIDE_GATES])
    parser.add_argument("--only", help="comma-separated case IDs, for example T48,R3")
    parser.add_argument("--repeat", type=int, default=1, help="consecutive runs (default 1)")
    parser.add_argument("--layers", help="comma-separated layers out of D,H,K,S")
    parser.add_argument("--reports", default=str(REPORTS_DIR), help="report directory")
    parser.add_argument("pytest_args", nargs="*", help="extra pytest arguments after --")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    catalogue = load_catalogue()
    for case_id in split_option(args.only):
        if case_id not in catalogue:
            parser.error(f"--only: unknown case {case_id}")
    for layer in split_option(args.layers):
        if layer not in HARNESS_LAYERS:
            parser.error(f"--layers: {layer} is not one of {', '.join(HARNESS_LAYERS)}")
    return args


def pytest_command(args: argparse.Namespace, raw_path: Path) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        ACCEPTANCE_DIR,
        "-p",
        "tests.b.harness.plugin",
        f"--wg-raw={raw_path}",
        "-q",
        "-rfE",
    ]
    for option, value in (("--wg-gate", args.gate), ("--wg-only", args.only)):
        if value:
            command.append(f"{option}={value}")
    if args.layers:
        command.append(f"--wg-layers={args.layers}")
    return command + list(args.pytest_args)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selection = Selection(
        gate=args.gate,
        only=split_option(args.only),
        layers=split_option(args.layers) or HARNESS_LAYERS,
    )
    runs = []
    with tempfile.TemporaryDirectory(prefix="wg-acceptance-") as tmp:
        for index in range(args.repeat):
            raw_path = Path(tmp) / f"run-{index + 1}.json"
            print(f"acceptance run {index + 1}/{args.repeat}", flush=True)
            completed = subprocess.run(pytest_command(args, raw_path), cwd=BACKEND_DIR, check=False)
            if not raw_path.exists():
                print(f"pytest exited with {completed.returncode} and wrote no results")
                return 1
            runs.append(json.loads(raw_path.read_text(encoding="utf-8")))
    report = build_report(load_catalogue(), runs, selection)
    json_path, md_path = write_report(report, Path(args.reports))
    print(f"acceptance report: {json_path}")
    print(f"acceptance report: {md_path}")
    verdict = report["gate_verdict"]
    if verdict:
        counts = f"{verdict['passed']}/{verdict['required']}"
        print(f"gate {verdict['gate']}: {verdict['result']} ({counts})")
    return exit_code(report, runs)


if __name__ == "__main__":
    raise SystemExit(main())
