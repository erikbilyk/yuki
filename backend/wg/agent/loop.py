"""One agent turn: model call with tool schemas, at most 8 tool calls, final text (SPEC §18.4).

The result carries the model's raw explanation and every tool outcome. Masking, provenance
checks and the reply check are applied by the caller, not here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from wg.agent.llm import ModelAdapter, ModelReply
from wg.agent.prompt import SystemPrompt, load_system_prompt
from wg.agent.registry import ToolContext, ToolOutcome, ToolRegistry
from wg.contracts.errors import ErrorCode
from wg.contracts.tools import ToolError

MAX_TOOL_CALLS = 8


@dataclass(frozen=True)
class TurnResult:
    """`explanation` is the model's text, not yet checked; `outcomes` are in call order."""

    explanation: str
    outcomes: tuple[ToolOutcome, ...]
    model_calls: int
    limit_reached: bool


def run_turn(
    adapter: ModelAdapter,
    registry: ToolRegistry,
    context: ToolContext,
    message: str,
    *,
    history: Sequence[Mapping[str, Any]] = (),
    prompt: SystemPrompt | None = None,
) -> TurnResult:
    """Play one customer message through the model and the registry."""
    prompt = prompt or load_system_prompt()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt.text},
        *(dict(item) for item in history),
        {"role": "user", "content": message},
    ]
    schemas = registry.schemas(context)
    outcomes: list[ToolOutcome] = []
    model_calls = 0
    executed = 0
    limit_reached = False
    while True:
        reply = adapter.complete(messages, schemas, prompt_version=prompt.version)
        model_calls += 1
        if not reply.tool_calls or limit_reached:
            return TurnResult(reply.text, tuple(outcomes), model_calls, limit_reached)
        messages.append(_assistant_message(reply))
        for call in reply.tool_calls:
            if executed < MAX_TOOL_CALLS:
                executed += 1
                outcome = registry.call(context, call.name, call.arguments)
            else:
                outcome = _over_limit(call.name)
            outcomes.append(outcome)
            messages.append(
                {"role": "tool", "tool_call_id": call.call_id, "content": outcome.payload()}
            )
        if executed >= MAX_TOOL_CALLS:
            # The budget is spent: one last call without tools makes the model answer in text.
            limit_reached = True
            schemas = []


def _assistant_message(reply: ModelReply) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": reply.text or None,
        "tool_calls": [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json},
            }
            for call in reply.tool_calls
        ],
    }
    if reply.reasoning:
        # Thinking mode requires the reasoning of a tool-calling turn to be sent back.
        message["reasoning_content"] = reply.reasoning
    return message


def _over_limit(name: str) -> ToolOutcome:
    error = ToolError(code=ErrorCode.UNSUPPORTED_REQUEST, detail={"reason": "tool_call_limit"})
    return ToolOutcome(name, None, error=error)
