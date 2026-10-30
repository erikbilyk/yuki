"""Command line: `python -m evals.runner live|comparison|score` (run from the repository root)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from evals.runner.episode import Episode, EpisodeSettings
from evals.runner.schema import COMPARISON_CATEGORY, Scenario, load_scenarios

from wg.settings import REPO_ROOT, read_env_file

DEFAULT_SCENARIOS = REPO_ROOT / "evals" / "scenarios"
DEFAULT_OUT = REPO_ROOT / "evals" / "out"
SYSTEMS = ("coordinated", "flat")
# Settings that describe the model; the manifest records these and nothing else from the env file.
MODEL_SETTINGS = ("WG_LLM_PROVIDER", "WG_LLM_MODEL", "WG_LLM_THINKING", "WG_LLM_BASE_URL")


@dataclass(frozen=True)
class Job:
    scenario: Scenario
    system: str
    repeat: int


def plan_jobs(mode: str, scenarios: list[Scenario], repeat: int) -> list[Job]:
    """Live trials run each scenario as written; the comparison runs both systems, interleaved."""
    if mode == "live":
        chosen = [s for s in scenarios if s.category != COMPARISON_CATEGORY]
        return [Job(s, s.system, r) for r in range(1, repeat + 1) for s in chosen]
    chosen = [s for s in scenarios if s.category == COMPARISON_CATEGORY]
    return [Job(s, system, r) for r in range(1, repeat + 1) for s in chosen for system in SYSTEMS]


def _git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return done.stdout.strip()


def _build_info() -> dict[str, Any]:
    return {
        "commit": _git("rev-parse", "HEAD") or None,
        "dirty": bool(_git("status", "--porcelain")),
    }


def _model_settings(env_file: Path | None, force_stub: bool) -> dict[str, str]:
    values = read_env_file(env_file) if env_file is not None else {}
    chosen = {name: values.get(name, "") for name in MODEL_SETTINGS}
    if force_stub:
        chosen["WG_LLM_PROVIDER"] = "stub"
    return chosen


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.runner")
    parser.add_argument("mode", choices=("live", "comparison", "score"))
    parser.add_argument("--repeat", type=int, help="episodes per scenario and system")
    parser.add_argument("--only", help="comma-separated scenario ids")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--env-file", type=Path, help="backend settings; default: .env if present")
    parser.add_argument("--stub", action="store_true", help="force the stub model provider")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--run-id")
    parser.add_argument("--run", help="score: run id under the output folder; default: newest")
    parser.add_argument("--budget-cny", help="stop starting episodes once this much is spent")
    parser.add_argument("--episode-timeout-s", type=float, default=900.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "score":
        return score(args)
    return execute(args)


def score(args: argparse.Namespace) -> int:
    runs = sorted(
        (path for path in args.out.glob("*/manifest.json")), key=lambda p: p.stat().st_mtime
    )
    if args.run:
        run_dir = args.out / args.run
    elif runs:
        run_dir = runs[-1].parent
    else:
        print(f"no run under {args.out}", file=sys.stderr)
        return 2
    if not (REPO_ROOT / "evals" / "score").is_dir():
        print("the scorer (evals/score) is not present", file=sys.stderr)
        return 2
    command = [sys.executable, "-m", "evals.score", str(run_dir)]
    return subprocess.run(command, check=False).returncode


def execute(args: argparse.Namespace) -> int:
    if not args.scenarios.is_dir():
        print(f"scenario folder not found: {args.scenarios}", file=sys.stderr)
        return 2
    scenarios = load_scenarios(args.scenarios)
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        unknown = wanted - {s.id for s in scenarios}
        if unknown:
            print(f"unknown scenario ids: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        scenarios = [s for s in scenarios if s.id in wanted]
    repeat = args.repeat or (3 if args.mode == "comparison" else 1)
    jobs = plan_jobs(args.mode, scenarios, repeat)
    if not jobs:
        print(f"no {args.mode} scenarios selected in {args.scenarios}", file=sys.stderr)
        return 2

    env_file = args.env_file
    if env_file is None and (REPO_ROOT / ".env").is_file():
        env_file = REPO_ROOT / ".env"
    started = datetime.now(UTC)
    build = _build_info()
    commit = (build["commit"] or "unknown")[:7]
    run_id = args.run_id or f"{args.mode}-{started.strftime('%Y%m%dT%H%M%SZ')}-{commit}"
    out_dir = args.out / run_id
    if out_dir.exists():
        print(f"run folder exists: {out_dir}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True)

    settings = EpisodeSettings(
        env_file=env_file,
        force_stub=args.stub,
        spend_cap_cny=None,
        episode_timeout_s=args.episode_timeout_s,
    )
    budget = Decimal(args.budget_cny) if args.budget_cny else None
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "mode": args.mode,
        "repeat": repeat,
        "scenarios_dir": str(args.scenarios),
        "model": _model_settings(env_file, args.stub),
        "build": build,
        "budget_cny": args.budget_cny,
        "started_at": started.isoformat(timespec="seconds"),
        "episodes": [],
    }
    spent = Decimal(0)
    failed = False
    for job in jobs:
        remaining = None if budget is None else budget - spent
        entry: dict[str, Any] = {"scenario_id": job.scenario.id, "system": job.system}
        if remaining is not None and remaining <= 0:
            episode_id = f"{job.scenario.id}.{job.system}.r{job.repeat}"
            entry.update(episode_id=episode_id, status="skipped", reason="budget spent")
            manifest["episodes"].append(entry)
            continue
        episode = Episode(job.scenario, job.system, job.repeat, out_dir, settings)
        result = episode.run(None if remaining is None else str(remaining))
        path = out_dir / "episodes" / f"{episode.episode_id}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        cost = Decimal(result["model"]["cost_cny"])
        spent += cost
        failed |= result["status"] != "completed"
        entry.update(
            episode_id=episode.episode_id,
            status=result["status"],
            turns=len(result["turns"]),
            cost_cny=str(cost),
            file=f"episodes/{episode.episode_id}.json",
        )
        manifest["episodes"].append(entry)
        manifest["cost_cny"] = str(spent)
        _write_manifest(out_dir, manifest)
        print(
            f"{episode.episode_id} {result['status']} turns={entry['turns']} cost_cny={cost}",
            flush=True,
        )
    manifest["cost_cny"] = str(spent)
    manifest["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    _write_manifest(out_dir, manifest)
    print(f"run {run_id}: {len(jobs)} episodes, cost_cny={spent}, output {out_dir}")
    return 1 if failed else 0


def _write_manifest(out_dir: Path, manifest: dict[str, Any]) -> None:
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    (out_dir / "manifest.json").write_text(text, encoding="utf-8")
