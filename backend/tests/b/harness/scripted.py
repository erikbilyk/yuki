"""Layer S: the real agent turn with the stub provider playing the model from a script file.

A script is a YAML list in `tests/b/scripts/` (SPEC §18.1). `${name}` placeholders take values
that a script cannot know in advance, such as a logical action id. The turn runs in this
process on the same application and database as the `WgClient`, so a case reads the
consequences through the client and the read-only database view.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

import yaml

from wg.agent.llm import ModelAdapter, ModelReply, StubProvider, call_log_path
from wg.agent.registry import Services, ToolOutcome, ToolRegistry
from wg.agent.turn import AgentTurn, agent_turn
from wg.contracts.enums import Channel
from wg.contracts.errors import ErrorCode
from wg.contracts.stub_script import StubScript
from wg.core.k7_verify import REPLACED_EVENT

from .client import MAIN_CUSTOMER, WgClient

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
EXTRA_READ_EVENT = "extra_read"
TOOL_REFUSED_EVENT = "tool_refused"
DRAFT_REFUSED_EVENT = "draft_refused"


class RecordingStub(StubProvider):
    """The stub provider that also keeps every message list it was sent."""

    def __init__(self, script_path: Path) -> None:
        super().__init__(script_path)
        self.payloads: list[str] = []

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelReply:
        self.payloads.append(json.dumps(list(messages), ensure_ascii=False))
        return super().complete(messages, tools)


def load_script(name: str, **values: Any) -> str:
    """The text of `scripts/<name>.yaml` with its placeholders filled; a missing value fails."""
    text = (SCRIPTS_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    filled = Template(text).substitute(
        {key: json.dumps(value, ensure_ascii=False) for key, value in values.items()}
    )
    StubScript.model_validate(yaml.safe_load(filled))
    return filled


@dataclass(frozen=True)
class Scanned:
    """One place a secret may have leaked to, and its text."""

    place: str
    text: str


class ScriptedAgent:
    """Customer `cus_main` talking to the agent of the application behind `wg`."""

    def __init__(self, wg: WgClient, workdir: Path) -> None:
        self.wg = wg
        self.workdir = workdir
        workdir.mkdir(parents=True, exist_ok=True)
        app = wg.http.app  # type: ignore[attr-defined]
        self.settings = app.state.settings
        self.registry = ToolRegistry(app.state.db, Services.from_state(app.state))
        self.log_path = call_log_path(self.settings)
        self.payloads: list[str] = []
        self.turns = 0

    def turn(
        self,
        text: str,
        script: str,
        *,
        history: Sequence[dict[str, str]] = (),
        **values: Any,
    ) -> AgentTurn:
        """One customer message, answered by the model of `script`."""
        self.turns += 1
        path = self.workdir / f"script-{self.turns}.yaml"
        path.write_text(load_script(script, **values), encoding="utf-8")
        stub = RecordingStub(path)
        adapter = ModelAdapter(stub, self.settings, log_path=self.log_path)
        try:
            return agent_turn(
                adapter,
                self.registry,
                customer_id=MAIN_CUSTOMER,
                channel=Channel.WEB,
                text=text,
                history=history,
            )
        finally:
            self.payloads.extend(stub.payloads)

    # What the turn left behind

    def audit(self, event: str) -> list[dict[str, Any]]:
        return self.wg.db.rows(
            "SELECT * FROM audit_log WHERE event = ? ORDER BY audit_id", (event,)
        )

    def replacements(self) -> int:
        return len(self.audit(REPLACED_EVENT))

    def extra_reads(self) -> int:
        return len(self.audit(EXTRA_READ_EVENT))

    def refusals(self, tool: str | None = None) -> list[tuple[str, str]]:
        """(tool, code) of every call the registry refused, in order, with extra reads."""
        rows = [*self.audit(TOOL_REFUSED_EVENT), *self.audit(EXTRA_READ_EVENT)]
        rows.sort(key=lambda row: row["audit_id"])
        return [(r["ref"], r["decision"]) for r in rows if tool is None or r["ref"] == tool]

    def refused_codes(self) -> list[str]:
        """The code of every refusal in the audit log, whichever layer wrote it."""
        events = (TOOL_REFUSED_EVENT, EXTRA_READ_EVENT, DRAFT_REFUSED_EVENT)
        rows = [row for event in events for row in self.audit(event)]
        rows.sort(key=lambda row: row["audit_id"])
        return [row["decision"] for row in rows]

    def actions(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is None:
            return self.wg.db.rows("SELECT * FROM actions ORDER BY created_at, logical_action_id")
        return self.wg.db.rows(
            "SELECT * FROM actions WHERE kind = ? ORDER BY created_at, logical_action_id", (kind,)
        )

    def params(self, kind: str) -> list[dict[str, Any]]:
        return [json.loads(row["params_json"]) for row in self.actions(kind)]

    # Secret scan (T7)

    def scan_places(self, caplog_text: str) -> list[Scanned]:
        """Database files, every file next to them (logs, llm_calls.jsonl) and provider payloads."""
        places = [Scanned("provider payloads", "\n".join(self.payloads))]
        places.append(Scanned("captured logs", caplog_text))
        db_path = self.wg.db.path
        for path in sorted(db_path.parent.rglob("*")):
            if path.is_file() and not path.name.endswith((".yaml", ".lock")):
                places.append(Scanned(path.name, path.read_bytes().decode("latin-1")))
        return places


def contains_token(secret: str, text: str) -> bool:
    """Whether `secret` appears as a whole token, not inside an id, a hash or a longer number."""
    return re.search(rf"(?<![0-9A-Za-z]){re.escape(secret)}(?![0-9A-Za-z])", text) is not None


def leaks(secrets: Sequence[str], places: Sequence[Scanned]) -> list[tuple[str, str]]:
    return [(p.place, s) for p in places for s in secrets if contains_token(s, p.text)]


def only(turn: AgentTurn) -> ToolOutcome:
    [outcome] = turn.turn.outcomes
    return outcome


def refused_with(outcome: ToolOutcome, *codes: ErrorCode) -> dict[str, Any]:
    """Assert the call was refused with one of `codes`; returns the refusal detail."""
    assert outcome.error is not None, f"{outcome.name} was not refused"
    assert outcome.error.code in codes, f"{outcome.name}: {outcome.error.code} not in {codes}"
    return dict(outcome.error.detail)
