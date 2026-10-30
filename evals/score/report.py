"""score.json and score.md for a scored run."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from .scorer import COMPARISON, SMOKE, Cell, Scored, cells, verdict
from .source import Run

SCHEMA_VERSION = 1
CATEGORIES = (
    "ambiguity",
    "missing_slots",
    "multi_step",
    "injected_text",
    "stale_data",
    "grounded_explanation",
    "colloquial",
    "out_of_scope",
)
SMALL_SAMPLE = 30


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _model_summary(scored: list[Scored]) -> dict[str, list[str]]:
    keys = (
        "requested_models",
        "returned_models",
        "thinking",
        "prompt_versions",
        "tool_schema_versions",
        "build_ids",
        "providers",
    )
    summary: dict[str, set[str]] = {key: set() for key in keys}
    for item in scored:
        for key in keys:
            summary[key].update(str(v) for v in item.model.get(key, []))
    return {key: sorted(values) for key, values in summary.items()}


def _totals(scored: list[Scored]) -> dict[str, Any]:
    outcomes = Counter(item.outcome for item in scored)
    return {
        "episodes": len(scored),
        "passed": outcomes["passed"],
        "failed": outcomes["failed"],
        "error": outcomes["error"],
        "unauthorized_effects": sum(len(i.unauthorized) for i in scored),
        "duplicate_effects": sum(i.duplicate_effects for i in scored),
        "k7_replacements": sum(i.metrics.k7_replacements for i in scored),
        "cost_cny": sum((i.metrics.cost_cny for i in scored), Decimal(0)),
    }


def _by_category(scored: list[Scored]) -> list[dict[str, Any]]:
    rows = []
    for category in CATEGORIES:
        items = [i for i in scored if i.category == category]
        if items:
            rows.append(
                {
                    "category": category,
                    **_totals(items),
                    "scenarios": sorted({i.scenario_id for i in items}),
                }
            )
    return rows


def _comparison(all_cells: list[Cell]) -> list[dict[str, Any]]:
    by_scenario: dict[str, dict[str, Cell]] = defaultdict(dict)
    for cell in all_cells:
        if cell.category == COMPARISON:
            by_scenario[cell.scenario_id][cell.system] = cell
    rows = []
    for scenario_id, systems in sorted(by_scenario.items()):
        coordinated, flat = systems.get("coordinated"), systems.get("flat")
        rows.append(
            {
                "scenario_id": scenario_id,
                "case": (coordinated or flat).case,
                "coordinated": asdict(coordinated) if coordinated else None,
                "flat": asdict(flat) if flat else None,
                "verdict": verdict(coordinated, flat),
            }
        )
    return rows


def build(run: Run, scored: list[Scored]) -> dict[str, Any]:
    live = [i for i in scored if i.group == "live"]
    smoke = [i for i in scored if i.group == SMOKE]
    comparison = [i for i in scored if i.group == COMPARISON]
    all_cells = cells(scored)
    return _plain(
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run.run_id,
            "mode": run.manifest.get("mode"),
            "build": run.manifest.get("build"),
            "model": _model_summary(scored),
            "totals": _totals(scored),
            "live": {"totals": _totals(live), "categories": _by_category(live)},
            "smoke": {"totals": _totals(smoke)},
            "comparison": {
                "totals": _totals(comparison),
                "systems": {
                    system: _totals([i for i in comparison if i.system == system])
                    for system in sorted({i.system for i in comparison})
                },
                "cases": _comparison(all_cells),
            },
            "cells": [asdict(cell) for cell in all_cells],
            "episodes": [
                {
                    "episode_id": i.episode_id,
                    "scenario_id": i.scenario_id,
                    "case": i.case,
                    "category": i.category,
                    "group": i.group,
                    "system": i.system,
                    "repeat": i.repeat,
                    "status": i.status,
                    "outcome": i.outcome,
                    "dispositions": list(i.dispositions),
                    "unauthorized_effects": list(i.unauthorized),
                    "duplicate_effects": i.duplicate_effects,
                    "failed_checks": [
                        {"name": c.name, "detail": c.detail}
                        for c in i.checks
                        if not c.ok
                    ],
                    "metrics": asdict(i.metrics),
                }
                for i in scored
            ],
        }
    )


def _fraction(passed: int, total: int) -> str:
    return f"{passed}/{total}"


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines + [""]


def _system_row(cell: dict[str, Any] | None) -> list[Any]:
    if cell is None:
        return ["-"] * 7
    return [
        _fraction(cell["passed"], cell["runs"]),
        cell["unauthorized"],
        cell["duplicates"],
        cell["customer_actions"],
        cell["turns"],
        cell["model_calls"],
        cell["cost_cny"],
    ]


def markdown(doc: dict[str, Any]) -> str:
    build_info = doc.get("build") or {}
    model = doc["model"]
    totals = doc["totals"]
    out = [f"# Evaluation score: {doc['run_id']}", ""]
    out += [
        (
            f"- Mode: {doc.get('mode')}; build {build_info.get('commit')} "
            f"(dirty: {build_info.get('dirty')})"
        ),
        (
            f"- Models requested: {', '.join(model['requested_models']) or '-'}; "
            f"returned: {', '.join(model['returned_models']) or '-'}; "
            f"thinking: {', '.join(model['thinking']) or '-'}; providers: {', '.join(model['providers']) or '-'}"
        ),
        (
            f"- Prompt versions: {', '.join(model['prompt_versions']) or '-'}; "
            f"tool schema versions: {', '.join(model['tool_schema_versions']) or '-'}"
        ),
        (
            f"- Episodes: {totals['episodes']}; passed {totals['passed']}, failed {totals['failed']}, "
            f"errors {totals['error']}"
        ),
        (
            f"- Unauthorized effects: {totals['unauthorized_effects']}; duplicate effects: "
            f"{totals['duplicate_effects']}; K7 replacements: {totals['k7_replacements']}"
        ),
        f"- Cost: ¥{totals['cost_cny']}",
        "",
    ]
    live = doc["live"]
    if live["totals"]["episodes"]:
        out += ["## Live trials", ""]
        out += _table(
            [
                "Category",
                "Scenarios",
                "Passed",
                "Unauthorized",
                "Duplicate",
                "K7 replaced",
                "Cost (¥)",
            ],
            [
                [
                    r["category"],
                    len(r["scenarios"]),
                    _fraction(r["passed"], r["episodes"]),
                    r["unauthorized_effects"],
                    r["duplicate_effects"],
                    r["k7_replacements"],
                    r["cost_cny"],
                ]
                for r in live["categories"]
            ],
        )
        if live["totals"]["episodes"] < SMALL_SAMPLE:
            out += [f"Counts only: fewer than {SMALL_SAMPLE} episodes.", ""]
    smoke = doc["smoke"]["totals"]
    if smoke["episodes"]:
        out += [
            "## Smoke",
            "",
            (
                f"{_fraction(smoke['passed'], smoke['episodes'])} passed; unauthorized "
                f"{smoke['unauthorized_effects']}, duplicate {smoke['duplicate_effects']}."
            ),
            "",
        ]
    comparison = doc["comparison"]
    if comparison["cases"]:
        out += ["## Coordinator against flat agent", ""]
        header = [
            "Case",
            "System",
            "Passed",
            "Unauth.",
            "Dup.",
            "Customer actions",
            "Turns",
            "Model calls",
            "Cost (¥)",
        ]
        rows = []
        for case in comparison["cases"]:
            for system in ("coordinated", "flat"):
                rows.append(
                    [
                        case["case"] or case["scenario_id"],
                        system,
                        *_system_row(case[system]),
                    ]
                )
        out += _table(header, rows)
        out += _table(
            ["Case", "Verdict"],
            [
                [c["case"] or c["scenario_id"], c["verdict"]]
                for c in comparison["cases"]
            ],
        )
        out += [
            (
                "Verdicts compare passed episodes, then unauthorized and duplicate effects; "
                "equal results are ties. Gateway protections are not credited to either system. "
                "Customer actions are messages plus confirmations plus codes entered, averaged "
                "per episode."
            ),
            "",
        ]
    problems = [e for e in doc["episodes"] if e["outcome"] != "passed"]
    if problems:
        out += ["## Episodes not passed", ""]
        for episode in problems:
            out.append(
                f"- `{episode['episode_id']}` ({episode['outcome']}): "
                + "; ".join(
                    f"{c['name']}" + (f" ({c['detail']})" if c["detail"] else "")
                    for c in episode["failed_checks"]
                )
            )
        out.append("")
    out += ["## Episodes", ""]
    out += _table(
        [
            "Episode",
            "Outcome",
            "Dispositions",
            "Unauth.",
            "Dup.",
            "Turns",
            "Model calls",
            "Customer actions",
            "Cost (¥)",
        ],
        [
            [
                e["episode_id"],
                e["outcome"],
                ", ".join(str(d) for d in e["dispositions"]) or "-",
                len(e["unauthorized_effects"]),
                e["duplicate_effects"],
                e["metrics"]["turns"],
                e["metrics"]["model_calls"],
                e["metrics"]["customer_actions"],
                e["metrics"]["cost_cny"],
            ]
            for e in doc["episodes"]
        ],
    )
    out += [
        (
            "Tool calls are not recorded by the backend; the tool rounds in `score.json` "
            "(model calls after the first in a turn) are a lower bound."
        ),
        "",
    ]
    return "\n".join(out)


def write(run: Run, scored: list[Scored]) -> dict[str, Any]:
    doc = build(run, scored)
    (run.path / "score.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (run.path / "score.md").write_text(markdown(doc), encoding="utf-8")
    return doc
