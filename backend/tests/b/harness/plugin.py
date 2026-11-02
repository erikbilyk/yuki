"""Pytest plugin for acceptance cases: selection by case, layer and gate; results; reports.

Load it with `-p tests.b.harness.plugin`. Each test carries `@pytest.mark.case("T1")` and a
`layer` mark, either directly or through a parametrized fixture (see tests/b/conftest.py).
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wg.api.app import create_app

from .catalogue import HARNESS_LAYERS, CatalogueError, load_catalogue
from .env import harness_settings
from .report import REPORTS_DIR, Selection, build_report, write_report

MESSAGE_LIMIT = 400


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("wg", "YUKI acceptance harness")
    group.addoption(
        "--wg-gate", default=None, help="run the cases a gate requires: G1, G2, G3, GS5"
    )
    group.addoption("--wg-only", default=None, help="comma-separated case IDs, for example T1,R3")
    group.addoption("--wg-layers", default=None, help="comma-separated layers, for example D,H")
    group.addoption("--wg-raw", default=None, help="write raw results to this file, no report")
    group.addoption("--wg-reports", default=str(REPORTS_DIR), help="directory for reports")


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(AcceptanceRecorder(config), "wg-acceptance-recorder")


def split_option(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


def detect_features() -> tuple[list[str], str | None]:
    """Features the backend advertises in GET /healthz (for example "s5")."""
    with tempfile.TemporaryDirectory(prefix="wg-features-") as tmp:
        try:
            app = create_app(harness_settings(Path(tmp)))
            with TestClient(app) as client:
                response = client.get("/healthz")
            response.raise_for_status()
            return list(response.json().get("features", [])), None
        # The report must still be written when the app cannot start; the error is recorded.
        except Exception as exc:
            return [], f"{type(exc).__name__}: {exc}"


def item_case(item: pytest.Item) -> str | None:
    marker = item.get_closest_marker("case")
    return str(marker.args[0]) if marker else None


def item_layer(item: pytest.Item) -> str | None:
    marker = item.get_closest_marker("layer")
    return str(marker.args[0]) if marker else None


def item_variant(item: pytest.Item, layer: str | None) -> str:
    """Test name plus its parameters, without the layer parameter."""
    name = getattr(item, "originalname", item.name)
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return name
    ids = [part for part in callspec.id.split("-") if part != layer]
    return f"{name}[{'-'.join(ids)}]" if ids else name


class AcceptanceRecorder:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.catalogue = load_catalogue()
        layers = split_option(config.getoption("--wg-layers")) or HARNESS_LAYERS
        self.selection = Selection(
            gate=config.getoption("--wg-gate"),
            only=split_option(config.getoption("--wg-only")),
            layers=layers,
        )
        for case_id in self.selection.only:
            self.catalogue[case_id]
        for layer in layers:
            if layer not in HARNESS_LAYERS:
                raise pytest.UsageError(f"--wg-layers: {layer} is not one of {HARNESS_LAYERS}")
        self.features, self.features_error = detect_features()
        self.items: dict[str, dict[str, Any]] = {}
        self.started = time.monotonic()
        self.report_paths: tuple[Path, Path] | None = None

    def _selected(self, case_id: str, layer: str) -> bool:
        case = self.catalogue[case_id]
        if not case.is_applicable(self.features) or not self.selection.includes(case_id, layer):
            return False
        gate = self.selection.gate
        if gate is None:
            return True
        required = self.catalogue.required_pairs(gate, self.features)
        if (case_id, layer) in required or (case_id, "*") in required:
            return True
        # G3 also runs every other core case and reports it, without requiring it.
        return gate == "G3" and case.is_core

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        keep, drop = [], []
        for item in items:
            case_id, layer = item_case(item), item_layer(item)
            if case_id is None or layer is None:
                drop.append(item)
                continue
            if case_id not in self.catalogue:
                raise CatalogueError(f"{item.nodeid}: unknown case {case_id}")
            if self._selected(case_id, layer):
                keep.append(item)
                self.items[item.nodeid] = {
                    "nodeid": item.nodeid,
                    "case": case_id,
                    "layer": layer,
                    "variant": item_variant(item, layer),
                    "outcome": "not run",
                    "duration_s": 0.0,
                    "message": None,
                }
            else:
                drop.append(item)
        if drop:
            config.hook.pytest_deselected(items=drop)
            items[:] = keep

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        entry = self.items.get(report.nodeid)
        if entry is None:
            return
        entry["duration_s"] = round(entry["duration_s"] + report.duration, 3)
        if report.failed:
            entry["outcome"] = "failed" if report.when == "call" else "error"
            entry["message"] = _short_message(report)
        elif report.skipped and entry["outcome"] == "not run":
            entry["outcome"] = "skipped"
            entry["message"] = _short_message(report)
        elif report.passed and report.when == "call" and entry["outcome"] == "not run":
            entry["outcome"] = "passed"

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        run = {
            "exit_status": int(exitstatus),
            "duration_s": round(time.monotonic() - self.started, 1),
            "features": self.features,
            "features_error": self.features_error,
            "selection": self.selection.as_dict(),
            "results": list(self.items.values()),
        }
        raw_path = self.config.getoption("--wg-raw")
        if raw_path:
            Path(raw_path).write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
            return
        report = build_report(self.catalogue, [run], self.selection)
        self.report_paths = write_report(report, Path(self.config.getoption("--wg-reports")))

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if self.report_paths:
            for path in self.report_paths:
                terminalreporter.write_line(f"acceptance report: {path}")


def _short_message(report: pytest.TestReport) -> str:
    text = report.longreprtext if report.longrepr else ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    errors = [line[1:].strip() for line in lines if line.startswith("E ")]
    message = " | ".join(errors[:3]) if errors else (lines[-1] if lines else "")
    return message[:MESSAGE_LIMIT]
