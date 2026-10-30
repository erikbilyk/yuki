"""Per-episode measures taken from the episode file: effort, usage and cost."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Metrics:
    turns: int
    model_calls: int
    tool_rounds: int
    tool_audit_events: int
    confirmations: int
    codes_entered: int
    declined: int
    customer_actions: int
    k7_replacements: int
    errors: int
    timeouts: int
    cost_cny: Decimal
    latency_ms: int


def _cost(value: Any) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal(0)


def episode_metrics(episode: dict[str, Any]) -> Metrics:
    """`customer_actions` = messages + confirmations + codes entered.

    A tool round is a model call that was followed by another call in the same turn, so the
    count is a lower bound of tool calls (the backend does not record their names or number).
    """
    turns = episode.get("turns", [])
    cards = episode.get("cards", [])
    codes = episode.get("codes", [])
    confirmations = sum(1 for card in cards if card.get("decision") == "approved")
    entered = sum(1 for code in codes if code.get("decision") == "entered")
    declined = sum(1 for card in cards if card.get("decision") == "declined") + sum(
        1 for code in codes if code.get("decision") == "declined"
    )
    calls = [int((turn.get("model") or {}).get("calls", 0)) for turn in turns]
    return Metrics(
        turns=len(turns),
        model_calls=sum(calls),
        tool_rounds=sum(max(count - 1, 0) for count in calls),
        tool_audit_events=sum(len(turn.get("tool_events", [])) for turn in turns),
        confirmations=confirmations,
        codes_entered=entered,
        declined=declined,
        customer_actions=len(turns) + confirmations + entered,
        k7_replacements=len(episode.get("k7_replacements", [])),
        errors=len(episode.get("errors", [])),
        timeouts=sum(
            1
            for step in episode.get("steps", [])
            if step.get("timeout") or step.get("timed_out")
        ),
        cost_cny=_cost((episode.get("model") or {}).get("cost_cny")),
        latency_ms=sum(int(turn.get("latency_ms") or 0) for turn in turns),
    )
