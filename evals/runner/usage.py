"""Model usage of one episode, from the backend's call records (`llm_calls.jsonl`)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

TOKEN_FIELDS = ("cache_hit_tokens", "cache_miss_tokens", "completion_tokens", "reasoning_tokens")


def read_calls(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    calls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            calls.append(json.loads(line))
    return calls


def summarize(calls: list[dict[str, Any]], *, brief: bool = False) -> dict[str, Any]:
    """Counts, tokens, CNY cost and latency of the given call records.

    `brief` leaves out the model, prompt and build names, which are the same for every turn.
    """
    tokens = dict.fromkeys(TOKEN_FIELDS, 0)
    cost = Decimal(0)
    for call in calls:
        cost += Decimal(call.get("cost_cny") or "0")
        for name in TOKEN_FIELDS:
            tokens[name] += int((call.get("usage") or {}).get(name) or 0)
    latencies = [call["latency_ms"] for call in calls if call.get("latency_ms") is not None]

    def distinct(name: str) -> list[Any]:
        return sorted({call[name] for call in calls if call.get(name) is not None}, key=str)

    summary = {
        "calls": len(calls),
        "errors": [call["error"] for call in calls if call.get("error")],
        "cost_cny": str(cost),
        "tokens": tokens,
        "latency_ms_total": sum(latencies),
        "latency_ms_max": max(latencies, default=0),
    }
    if brief:
        return summary
    return summary | {
        "providers": distinct("provider"),
        "requested_models": distinct("requested_model"),
        "returned_models": distinct("returned_model"),
        "thinking": distinct("thinking"),
        "prompt_versions": distinct("prompt_version"),
        "tool_schema_versions": distinct("tool_schema_version"),
        "build_ids": distinct("build_id"),
    }
