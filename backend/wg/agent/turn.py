"""A customer message through ingress masking and the agent loop (SPEC §18.4, §18.6).

The raw text exists only inside `agent_turn`: the model, the tools, the audit log and the
caller all receive the masked text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from wg.agent.llm import ModelAdapter
from wg.agent.loop import TurnResult, run_turn
from wg.agent.registry import BOTH_CAPABILITIES, ToolContext, ToolRegistry
from wg.contracts.enums import Channel
from wg.contracts.tools import Capability, DataSource
from wg.core.k2_context import challenge_open, mask_ingress


@dataclass(frozen=True)
class AgentTurn:
    """`masked_text` is the customer's message as it may be stored and shown again."""

    masked_text: str
    turn: TurnResult


def agent_turn(
    adapter: ModelAdapter,
    registry: ToolRegistry,
    *,
    customer_id: str,
    channel: Channel,
    text: str,
    history: Sequence[Mapping[str, Any]] = (),
    capabilities: frozenset[Capability] = BOTH_CAPABILITIES,
    read_scope: frozenset[DataSource] | None = None,
) -> AgentTurn:
    """Mask the message, then play it through the model; `history` must already be masked."""
    with registry.db.read() as conn:
        ingress = mask_ingress(text, challenge_open=challenge_open(conn, customer_id))
    context = ToolContext(
        customer_id=customer_id,
        channel=channel,
        capabilities=capabilities,
        read_scope=read_scope,
        utterance=ingress.text,
    )
    result = run_turn(adapter, registry, context, ingress.text, history=history)
    return AgentTurn(masked_text=ingress.text, turn=result)
