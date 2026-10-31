"""Scenario files (YAML): one customer script, the system it runs on and what the scorer expects."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from wg.contracts.api import (
    EbillRequest,
    ExternalDebitRequest,
    IncomingPaymentRequest,
    SimSmsRequest,
)

Category = Literal[
    "ambiguity",
    "missing_slots",
    "multi_step",
    "injected_text",
    "stale_data",
    "grounded_explanation",
    "colloquial",
    "out_of_scope",
    "comparison",
]
COMPARISON_CATEGORY = "comparison"

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Allowed(_Strict):
    """One thing the customer may approve: an action kind (or PLAN) and the terms it must have.

    A card whose terms carry `amount_fen` needs `amount_fen` or `max_amount_fen` here; there is
    no entry that approves any amount. A PLAN entry lists the nodes its card may contain.
    """

    kind: str
    amount_fen: Annotated[int, Field(ge=0)] | None = None
    max_amount_fen: Annotated[int, Field(ge=0)] | None = None
    shares: str | None = None
    payee_id: str | None = None
    product_id: str | None = None
    nodes: tuple[Allowed, ...] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.amount_fen is not None and self.max_amount_fen is not None:
            raise ValueError("give amount_fen or max_amount_fen, not both")
        if self.nodes is not None and self.kind != "PLAN":
            raise ValueError("nodes belong to a PLAN entry")
        if self.kind == "PLAN" and not self.nodes:
            raise ValueError("a PLAN entry lists its nodes")
        return self


class Say(_Strict):
    text: Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class ApproveCards(_Strict):
    allow: Annotated[tuple[Allowed, ...], Field(min_length=1)]


class Withhold(_Strict):
    """An action whose code the customer does not enter in one `enter_code` step.

    Fields left out match any value, so `{kind: PURCHASE}` holds back every purchase code.
    """

    kind: str
    amount_fen: Annotated[int, Field(ge=0)] | None = None
    shares: str | None = None
    payee_id: str | None = None
    product_id: str | None = None

    @model_validator(mode="after")
    def _an_action_kind(self) -> Self:
        if self.kind == "PLAN":
            raise ValueError("a code belongs to an action; name the action kind, not PLAN")
        return self


class EnterCode(_Strict):
    """Read the code of each open challenge from GET /v1/sms and verify it (allow-listed only).

    `withhold` lists actions whose code is neither requested nor entered in this step even
    though an earlier allow-list covers them; a later `enter_code` without it enters them.
    """

    withhold: tuple[Withhold, ...] = ()


class AdvanceTo(_Strict):
    """Move the simulated clock to `to`, or forward by `days` and `minutes`."""

    to: str | None = None
    days: Annotated[int, Field(ge=0)] | None = None
    minutes: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        relative = self.days is not None or self.minutes is not None
        if relative == (self.to is not None):
            raise ValueError("give either `to` or `days`/`minutes`")
        return self


class KillRestart(_Strict):
    """End the backend (`kill`: SIGKILL; `crash`: POST /sim/crash) and start it on the database."""

    how: Literal["kill", "crash"] = "kill"


class Fault(_Strict):
    fault: str
    count: Annotated[int, Field(ge=1)] = 1
    persistent: bool = False


# The simulator routes a scenario may call, with the contract each body must satisfy.
SIM_POSTS: dict[str, type[BaseModel]] = {
    "/sim/bills/ebill": EbillRequest,
    "/sim/sms": SimSmsRequest,
    "/sim/incoming-payment": IncomingPaymentRequest,
    "/sim/external-debit": ExternalDebitRequest,
}


class Sim(_Strict):
    """Inject one trusted or untrusted input as the demo panel would: `POST <post>` with `body`.

    The body is checked against the route's request contract and kept in its JSON form, so a
    YAML date (`due_date: 2026-11-11`) becomes the string the route expects.
    """

    post: str
    body: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def _body_fits_route(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        contract = SIM_POSTS.get(raw.get("post", ""))
        if contract is None:
            raise ValueError(f"unknown route {raw.get('post')!r}; expected {', '.join(SIM_POSTS)}")
        try:
            checked = contract.model_validate(raw.get("body"))
        except ValidationError as error:
            raise ValueError(f"body does not fit {raw['post']}: {error}") from error
        return {**raw, "body": checked.model_dump(mode="json")}


class SettleDays(_Strict):
    """Change the settlement period of every product, for example after a plan was approved.

    The runner writes `products` in the episode database itself; no route does this. The
    change is not a product event and leaves no audit row.
    """

    redemption: Annotated[int, Field(ge=0, le=30)] | None = None
    purchase: Annotated[int, Field(ge=0, le=30)] | None = None

    @model_validator(mode="after")
    def _something_to_set(self) -> Self:
        if self.redemption is None and self.purchase is None:
            raise ValueError("give redemption, purchase or both")
        return self


class WaitForStatus(_Strict):
    """Poll until the newest action of `action_kind`, or the newest plan, has one of `status`.

    A timeout is recorded on the step and the script goes on; the scorer judges the outcome.
    """

    action_kind: str | None = None
    plan: Literal["last"] | None = None
    status: Annotated[tuple[str, ...], Field(min_length=1)]
    timeout_s: Annotated[float, Field(gt=0, le=600)] = 30
    poll_s: Annotated[float, Field(gt=0, le=10)] = 0.5

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        if (self.action_kind is None) == (self.plan is None):
            raise ValueError("give either action_kind or plan")
        return self


STEP_KINDS: dict[str, type[_Strict]] = {
    "say": Say,
    "approve_cards": ApproveCards,
    "enter_code": EnterCode,
    "advance_to": AdvanceTo,
    "kill_restart": KillRestart,
    "fault": Fault,
    "sim": Sim,
    "settle_days": SettleDays,
    "wait_for_status": WaitForStatus,
}


class Step(_Strict):
    """A single-key mapping, for example `- say: "..."` or `- enter_code: {}`."""

    kind: str
    body: _Strict

    @model_validator(mode="before")
    @classmethod
    def _from_mapping(cls, raw: Any) -> Any:
        if isinstance(raw, Step):
            return raw
        if not isinstance(raw, dict) or len(raw) != 1:
            raise ValueError("a step is a mapping with exactly one key")
        ((kind, body),) = raw.items()
        model = STEP_KINDS.get(kind)
        if model is None:
            raise ValueError(f"unknown step {kind!r}; expected one of {', '.join(STEP_KINDS)}")
        if kind == "say" and isinstance(body, str):
            body = {"text": body}
        return {"kind": kind, "body": model.model_validate(body or {})}


class Scenario(_Strict):
    id: Identifier
    category: Category
    case: str | None = None
    title: str | None = None
    system: Literal["coordinated", "flat"] = "coordinated"
    customer: str = "cus_main"
    profile: str = "fixture_v1"
    variant: str | None = None
    stub_script: str | None = None
    steps: Annotated[tuple[Step, ...], Field(min_length=1)]
    expectations: dict[str, Any] = Field(default_factory=dict)


def load_scenario(path: Path) -> Scenario:
    """Read one scenario file; `stub_script` becomes an absolute path next to the file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    scenario = Scenario.model_validate(raw)
    if scenario.stub_script is not None:
        script = (path.parent / scenario.stub_script).resolve()
        scenario = scenario.model_copy(update={"stub_script": str(script)})
    return scenario


def load_scenarios(directory: Path) -> list[Scenario]:
    """Every `*.yaml` under `directory`, in file name order; ids must be unique."""
    scenarios = [load_scenario(path) for path in sorted(directory.rglob("*.yaml"))]
    seen: set[str] = set()
    for scenario in scenarios:
        if scenario.id in seen:
            raise ValueError(f"duplicate scenario id {scenario.id!r}")
        seen.add(scenario.id)
    return scenarios
