"""Plan composition request and the plan validator's verdict (SPEC §15.1, §18.2)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StrictBool, model_validator

from wg.contracts._base import ContractModel
from wg.contracts.errors import ErrorCode
from wg.contracts.ids import LogicalActionId

MAX_PLAN_ACTIONS = 12


class ComposePlanRequest(ContractModel):
    """Drafts to join into one plan; `depends_on` maps an action to the actions it waits for.

    Only the shape is checked here. Unknown references, cycles and funding are the
    validator's codes (`NOT_IN_PLAN`, `PLAN_CYCLE`, `UNFUNDED_DEPENDENCY`, ...).
    """

    draft_ids: Annotated[
        tuple[LogicalActionId, ...], Field(min_length=1, max_length=MAX_PLAN_ACTIONS)
    ]
    depends_on: dict[LogicalActionId, tuple[LogicalActionId, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_drafts(self) -> ComposePlanRequest:
        if len(set(self.draft_ids)) != len(self.draft_ids):
            raise ValueError("draft_ids must not repeat")
        return self


class PlanValidationResult(ContractModel):
    """`ok` holds exactly when no code was found; codes keep the validator's order."""

    ok: StrictBool
    codes: tuple[ErrorCode, ...] = ()

    @model_validator(mode="after")
    def _ok_without_codes(self) -> PlanValidationResult:
        if self.ok == bool(self.codes):
            raise ValueError("a plan is valid exactly when it has no codes")
        if len(set(self.codes)) != len(self.codes):
            raise ValueError("codes must not repeat")
        return self
