"""Acceptance report: case status from raw pytest runs, gate verdict, JSON and Markdown files.

A raw run is what the plugin records for one pytest session: the selection, the features
the backend advertised, and one entry per test with its case, layer, variant and outcome.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from wg.settings import REPO_ROOT

from .catalogue import GATE_ORDER, HARNESS_LAYERS, SIDE_GATES, Case, Catalogue
from .env import FIXTURE_PATH

REPORTS_DIR = REPO_ROOT / "var" / "reports"
REPRODUCIBLE_RUNS = 3

NOT_APPLICABLE = "not applicable"
NOT_RUN = "not run"
SPECIFIED = "specified"
IMPLEMENTED = "implemented"
PASSED = "passed"
REPRODUCIBLE = "reproducible"
STATUS_ORDER = (NOT_RUN, SPECIFIED, IMPLEMENTED, PASSED, REPRODUCIBLE)


@dataclass(frozen=True)
class Selection:
    gate: str | None = None
    only: tuple[str, ...] = ()
    layers: tuple[str, ...] = HARNESS_LAYERS

    def as_dict(self) -> dict[str, Any]:
        return {"gate": self.gate, "only": list(self.only), "layers": list(self.layers)}

    def includes(self, case_id: str, layer: str) -> bool:
        return (not self.only or case_id in self.only) and layer in self.layers


# Build information


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, check=False
    )


def build_id() -> str:
    result = _git("describe", "--always", "--dirty")
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def freeze_state() -> tuple[str | None, bool]:
    """Newest freeze-n tag and whether backend/ and frontend/ are unchanged since it."""
    tags = _git("tag", "--list", "freeze-*", "--sort=-v:refname").stdout.split()
    if not tags:
        return None, False
    return tags[0], _git("diff", "--quiet", tags[0], "--", "backend", "frontend").returncode == 0


def fixture_hash() -> str:
    return hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()


def environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "pytest": pytest.__version__,
        "platform": platform.platform(),
        "executable": sys.executable,
        "llm_provider": "stub",
    }


# Status


def _variant_outcomes(runs: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, list[str]]]]:
    """case -> layer -> variant -> outcome per run (missing in a run counts as not run)."""
    table: dict[str, dict[str, dict[str, list[str]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for index, run in enumerate(runs):
        for result in run["results"]:
            outcomes = table[result["case"]][result["layer"]][result["variant"]]
            outcomes.extend(["not run"] * (index - len(outcomes)))
            outcomes.append(result["outcome"])
    for layers in table.values():
        for variants in layers.values():
            for outcomes in variants.values():
                outcomes.extend(["not run"] * (len(runs) - len(outcomes)))
    return table


def _layer_result(variants: dict[str, list[str]] | None) -> str:
    if not variants:
        return "missing"
    outcomes = [outcome for runs in variants.values() for outcome in runs]
    if any(outcome in ("failed", "error") for outcome in outcomes):
        return "failed"
    if all(outcome == "passed" for outcome in outcomes):
        return "passed"
    return "incomplete"


def _case_status(
    case: Case,
    layers: dict[str, dict[str, list[str]]],
    selection: Selection,
    run_count: int,
    frozen: bool,
) -> tuple[str, dict[str, str]]:
    harness = [layer for layer in case.layers if layer in HARNESS_LAYERS] or sorted(layers)
    considered = [layer for layer in harness if selection.includes(case.case_id, layer)]
    results = {layer: _layer_result(layers.get(layer)) for layer in considered}
    if not considered:
        return NOT_RUN, results
    if all(result == "missing" for result in results.values()):
        return SPECIFIED, results
    if not all(result == "passed" for result in results.values()):
        return IMPLEMENTED, results
    if set(considered) != set(harness):
        return IMPLEMENTED, results
    if frozen and run_count >= REPRODUCIBLE_RUNS:
        return REPRODUCIBLE, results
    return PASSED, results


def _pair_result(
    table: dict[str, dict[str, dict[str, list[str]]]], case_id: str, layer: str
) -> str:
    layers = table.get(case_id, {})
    if layer == "*":
        merged = {f"{lay}:{var}": runs for lay, vs in layers.items() for var, runs in vs.items()}
        return _layer_result(merged)
    return _layer_result(layers.get(layer))


def gate_verdict(
    catalogue: Catalogue,
    table: dict[str, dict[str, dict[str, list[str]]]],
    gate: str,
    features: list[str],
    selection: Selection,
) -> dict[str, Any]:
    pairs = sorted(
        (case_id, layer)
        for case_id, layer in catalogue.required_pairs(gate, features)
        if layer == "*" or selection.includes(case_id, layer)
    )
    results = {f"{c}/{lay}": _pair_result(table, c, lay) for c, lay in pairs}
    failed = sorted(key for key, result in results.items() if result == "failed")
    missing = sorted(
        key for key, result in results.items() if result != "failed" and result != "passed"
    )
    external = sorted(f"{c}/{lay}" for c, lay in catalogue.external_pairs(gate))
    return {
        "gate": gate,
        "required": len(pairs),
        "passed": len(pairs) - len(failed) - len(missing),
        "failed": failed,
        "missing": missing,
        "external_not_run": external,
        "result": "passed" if not failed and not missing else "failed",
    }


def build_report(
    catalogue: Catalogue, runs: list[dict[str, Any]], selection: Selection
) -> dict[str, Any]:
    features = sorted({feature for run in runs for feature in run.get("features", [])})
    feature_errors = sorted({run["features_error"] for run in runs if run.get("features_error")})
    freeze_tag, frozen = freeze_state()
    table = _variant_outcomes(runs)
    unknown = sorted(case_id for case_id in table if case_id not in catalogue)

    cases: dict[str, Any] = {}
    for case in catalogue.cases.values():
        applicable = case.is_applicable(features)
        layers = table.get(case.case_id, {})
        if applicable:
            status, layer_results = _case_status(case, layers, selection, len(runs), frozen)
        else:
            status, layer_results = NOT_APPLICABLE, {}
        cases[case.case_id] = {
            "title": case.title,
            "set": case.set,
            "priority": case.priority,
            "gate": case.gate,
            "layers": list(case.layers),
            "applicable": applicable,
            "reason": case.reason,
            "status": status,
            "layer_results": layer_results,
            "external_layers": [layer for layer in case.layers if layer not in HARNESS_LAYERS],
            "variants": {
                layer: {name: outcomes for name, outcomes in sorted(variants.items())}
                for layer, variants in sorted(layers.items())
            },
            "messages": _messages(runs, case.case_id),
        }

    gates = {
        gate: gate_verdict(catalogue, table, gate, features, Selection())
        for gate in GATE_ORDER[:3] + SIDE_GATES
        if gate not in SIDE_GATES or "s5" in features
    }
    verdict = (
        gate_verdict(catalogue, table, selection.gate, features, selection)
        if selection.gate
        else None
    )
    failed_tests = sorted(
        {
            result["nodeid"]
            for run in runs
            for result in run["results"]
            if result["outcome"] in ("failed", "error")
        }
    )
    return {
        "build": build_id(),
        "freeze_tag": freeze_tag,
        "frozen": frozen,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment": environment(),
        "fixture_hash": fixture_hash(),
        "selection": selection.as_dict(),
        "repeat": len(runs),
        "runs": [
            {"exit_status": run["exit_status"], "duration_s": run["duration_s"]} for run in runs
        ],
        "features": features,
        "features_error": feature_errors,
        "unknown_cases": unknown,
        "cases": cases,
        "totals": {"by_set": _totals_by_set(catalogue, cases), "by_gate": gates},
        "gate_verdict": verdict,
        "failed_tests": failed_tests,
    }


def _messages(runs: list[dict[str, Any]], case_id: str) -> list[str]:
    seen: dict[str, None] = {}
    for run in runs:
        for result in run["results"]:
            if result["case"] == case_id and result.get("message"):
                seen.setdefault(f"{result['layer']} {result['variant']}: {result['message']}")
    return list(seen)[:20]


def _totals_by_set(catalogue: Catalogue, cases: dict[str, Any]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {}
    for set_name in catalogue.sets:
        members = [c for c in cases.values() if c["set"] == set_name]
        applicable = [c for c in members if c["applicable"]]

        def at_least(status: str, members: list[dict[str, Any]] = applicable) -> int:
            rank = STATUS_ORDER.index(status)
            return sum(1 for c in members if STATUS_ORDER.index(c["status"]) >= rank)

        totals[set_name] = {
            "cases": len(members),
            "p0": sum(1 for c in members if c["priority"] == "P0"),
            "applicable": len(applicable),
            "not_applicable": len(members) - len(applicable),
            "implemented": at_least(IMPLEMENTED),
            "passed": at_least(PASSED),
            "reproducible": at_least(REPRODUCIBLE),
        }
    return totals


def exit_code(report: dict[str, Any], runs: list[dict[str, Any]]) -> int:
    """1 when a required case failed (or, with a gate, is missing) or pytest itself failed."""
    if any(run["exit_status"] not in (0, 1, 5) for run in runs) or report["unknown_cases"]:
        return 1
    verdict = report["gate_verdict"]
    if verdict is not None:
        return 0 if verdict["result"] == "passed" else 1
    return 1 if report["failed_tests"] else 0


# Files


def write_report(report: dict[str, Any], directory: Path = REPORTS_DIR) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = report["generated_at"].replace("-", "").replace(":", "")
    build = report["build"].replace("/", "_")
    stem = directory / f"acceptance-{build}-{stamp}"
    json_path, md_path = stem.with_suffix(".json"), stem.with_suffix(".md")
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def render_markdown(report: dict[str, Any]) -> str:
    sel = report["selection"]
    lines = [
        "# Acceptance report",
        "",
        f"- Build: `{report['build']}`; frozen build: {'yes' if report['frozen'] else 'no'}"
        f" (tag: {report['freeze_tag'] or 'none'})",
        f"- Generated (UTC): {report['generated_at']}; runs: {report['repeat']}",
        f"- Selection: gate {sel['gate'] or '-'}; only {', '.join(sel['only']) or 'all'};"
        f" layers {', '.join(sel['layers'])}",
        f"- Environment: Python {report['environment']['python']},"
        f" pytest {report['environment']['pytest']}, {report['environment']['platform']}",
        f"- Fixture hash: `{report['fixture_hash']}`",
        f"- Features: {', '.join(report['features']) or 'none'}",
    ]
    for error in report["features_error"]:
        lines.append(f"- Feature detection failed: {error}")
    for case_id in report["unknown_cases"]:
        lines.append(f"- Test names unknown case {case_id}")

    verdict = report["gate_verdict"]
    if verdict:
        lines += [
            "",
            f"## Gate {verdict['gate']}: {verdict['result']}",
            "",
            f"Required case-layer pairs: {verdict['required']}; passed {verdict['passed']};"
            f" failed {len(verdict['failed'])}; missing {len(verdict['missing'])}.",
        ]
        if verdict["failed"]:
            lines.append(f"Failed: {', '.join(verdict['failed'])}.")
        if verdict["missing"]:
            lines.append(f"Missing or incomplete: {', '.join(verdict['missing'])}.")
        if verdict["external_not_run"]:
            lines.append(
                f"Not run by this harness (U, L): {', '.join(verdict['external_not_run'])}."
            )

    lines += [
        "",
        "## Totals by set",
        "",
        "| Set | Cases | P0 | Applicable | Implemented | Passed | Reproducible | Not applicable |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, t in report["totals"]["by_set"].items():
        lines.append(
            f"| {name} | {t['cases']} | {t['p0']} | {t['applicable']} | {t['implemented']}"
            f" | {t['passed']} | {t['reproducible']} | {t['not_applicable']} |"
        )

    lines += ["", "## Cases run", "", "| Case | Set | P | Status | Layers | Variants |"]
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for case_id, case in report["cases"].items():
        if case["status"] in (NOT_RUN, NOT_APPLICABLE):
            continue
        layer_text = ", ".join(f"{k} {v}" for k, v in case["layer_results"].items())
        variants = sum(len(v) for v in case["variants"].values())
        lines.append(
            f"| {case_id} | {case['set']} | {case['priority'] or '-'} | {case['status']}"
            f" | {layer_text} | {variants} |"
        )

    failures = [(cid, c) for cid, c in report["cases"].items() if c["messages"]]
    if failures:
        lines += ["", "## Failures", ""]
        for case_id, case in failures:
            for message in case["messages"]:
                lines.append(f"- {case_id} {message}")

    excluded = [(cid, c) for cid, c in report["cases"].items() if c["status"] == NOT_APPLICABLE]
    if excluded:
        lines += ["", "## Not applicable", ""]
        reasons: dict[str, list[str]] = defaultdict(list)
        for case_id, case in excluded:
            reasons[case["reason"] or "no reason given"].append(case_id)
        for reason, ids in reasons.items():
            lines.append(f"- {_id_ranges(ids)}: {reason}")
    return "\n".join(lines) + "\n"


def _id_ranges(ids: list[str]) -> str:
    """T78, T79, T80 -> T78–T80; other IDs are listed as they are."""
    parts: list[str] = []
    run: list[int] = []

    def flush() -> None:
        if run:
            parts.append(f"T{run[0]}" if len(run) == 1 else f"T{run[0]}–T{run[-1]}")
            run.clear()

    for case_id in ids:
        number = case_id[1:]
        if case_id.startswith("T") and number.isdigit():
            if run and int(number) != run[-1] + 1:
                flush()
            run.append(int(number))
        else:
            flush()
            parts.append(case_id)
    flush()
    return ", ".join(parts)
