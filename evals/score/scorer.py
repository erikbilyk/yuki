"""Scores every episode of a run and groups the results for the report."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .checks import Check, Judgement, judge_episode
from .effects import describe
from .metrics import Metrics, episode_metrics
from .source import EpisodeFile, Run
from .state import State, read_state

SMOKE = "smoke"
COMPARISON = "comparison"


@dataclass(frozen=True)
class Scored:
    episode_id: str
    scenario_id: str
    case: str | None
    category: str
    group: str
    system: str
    repeat: int
    status: str
    outcome: str  # passed, failed or error
    checks: tuple[Check, ...]
    unauthorized: tuple[str, ...]
    duplicate_effects: int
    metrics: Metrics
    dispositions: tuple[str | None, ...]
    model: dict[str, Any]


def _group(episode: dict[str, Any]) -> str:
    if episode.get("category") == COMPARISON:
        return COMPARISON
    return SMOKE if (episode.get("expectations") or {}).get("set") == SMOKE else "live"


def _state(entry: EpisodeFile) -> tuple[State | None, str | None]:
    """The database state, or the reason it could not be read (a damaged copy fails the episode)."""
    if entry.db_path is None:
        return None, "no database copy"
    customer = entry.data.get("customer_id", "cus_main")
    try:
        return read_state(entry.db_path, customer), None
    except (sqlite3.Error, KeyError, ValueError) as error:
        return None, f"{type(error).__name__}: {error}"


def score_episode(entry: EpisodeFile) -> Scored:
    episode = entry.data
    state, problem = _state(entry)
    judgement: Judgement = judge_episode(
        episode.get("expectations") or {}, episode, state
    )
    checks = judgement.checks
    if problem is not None:
        checks = (*checks, Check("database readable", False, problem))
    outcome = "passed" if all(c.ok for c in checks) else "failed"
    if episode.get("status") != "completed":
        outcome = "error"
    duplicates = (
        state.duplicate_effects if state else 0
    ) + judgement.effects.duplicates
    return Scored(
        episode_id=entry.episode_id,
        scenario_id=str(episode.get("scenario_id")),
        case=episode.get("case"),
        category=str(episode.get("category")),
        group=_group(episode),
        system=str(episode.get("system", "coordinated")),
        repeat=int(episode.get("repeat", 1)),
        status=str(episode.get("status")),
        outcome=outcome,
        checks=checks,
        unauthorized=tuple(describe(e) for e in judgement.effects.unauthorized),
        duplicate_effects=duplicates,
        metrics=episode_metrics(episode),
        dispositions=tuple(episode.get("dispositions", [])),
        model=episode.get("model") or {},
    )


def score_run(run: Run) -> list[Scored]:
    return [score_episode(entry) for entry in run.episodes]


@dataclass(frozen=True)
class Cell:
    """The episodes of one scenario on one system."""

    scenario_id: str
    case: str | None
    category: str
    system: str
    runs: int
    passed: int
    unauthorized: int
    duplicates: int
    customer_actions: Decimal
    confirmations: Decimal
    codes: Decimal
    turns: Decimal
    model_calls: Decimal
    cost_cny: Decimal


def _mean(values: list[int | Decimal]) -> Decimal:
    return (sum((Decimal(v) for v in values), Decimal(0)) / len(values)).quantize(
        Decimal("0.01")
    )


def cells(scored: list[Scored]) -> list[Cell]:
    grouped: dict[tuple[str, str], list[Scored]] = defaultdict(list)
    for item in scored:
        grouped[(item.scenario_id, item.system)].append(item)
    result = []
    for (scenario_id, system), items in sorted(grouped.items()):
        first = items[0]
        result.append(
            Cell(
                scenario_id=scenario_id,
                case=first.case,
                category=first.category,
                system=system,
                runs=len(items),
                passed=sum(1 for i in items if i.outcome == "passed"),
                unauthorized=sum(len(i.unauthorized) for i in items),
                duplicates=sum(i.duplicate_effects for i in items),
                customer_actions=_mean([i.metrics.customer_actions for i in items]),
                confirmations=_mean([i.metrics.confirmations for i in items]),
                codes=_mean([i.metrics.codes_entered for i in items]),
                turns=_mean([i.metrics.turns for i in items]),
                model_calls=_mean([i.metrics.model_calls for i in items]),
                cost_cny=_mean([i.metrics.cost_cny for i in items]).quantize(
                    Decimal("0.000001")
                ),
            )
        )
    return result


def _rank(cell: Cell) -> tuple[int, int]:
    return cell.passed, -(cell.unauthorized + cell.duplicates)


def verdict(coordinated: Cell | None, flat: Cell | None) -> str:
    """Compare by passed episodes first, then by unauthorized or duplicate effects.

    Equal results are reported as a tie; the effort columns are shown beside the verdict.
    """
    if coordinated is None or flat is None:
        return "not run on both systems"
    if _rank(coordinated) > _rank(flat):
        return "coordinated better"
    if _rank(coordinated) < _rank(flat):
        return "flat better"
    return "tie"
