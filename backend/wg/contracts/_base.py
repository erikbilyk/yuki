"""Base model shared by every contract type."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    """Immutable value with no unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)
