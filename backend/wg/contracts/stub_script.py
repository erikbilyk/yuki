"""Schema of the scripted model turns replayed by the stub provider (SPEC §18.1).

The file named by WG_LLM_STUB_SCRIPT is a YAML list; each item is one model call,
either `{tool_calls: [{name, arguments}]}` or `{text: "..."}`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import ConfigDict, Field, RootModel

from wg.contracts._base import ContractModel


class StubToolCall(ContractModel):
    name: Annotated[str, Field(min_length=1)]
    arguments: dict[str, Any] = Field(default_factory=dict)


class StubToolCallsTurn(ContractModel):
    tool_calls: Annotated[tuple[StubToolCall, ...], Field(min_length=1)]


class StubTextTurn(ContractModel):
    text: str


StubTurn = StubToolCallsTurn | StubTextTurn


class StubScript(RootModel[tuple[StubTurn, ...]]):
    """The turns in the order they are consumed, one per model call."""

    model_config = ConfigDict(frozen=True)
