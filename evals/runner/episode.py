"""One episode: a scenario played against a fresh backend, recorded as JSON."""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from evals.runner import policy
from evals.runner.backend import Backend, BackendError, backend_environment, free_port
from evals.runner.dbview import DbView, copy_database, copy_file
from evals.runner.http import CHAT_TIMEOUT_S, Session
from evals.runner.schema import (
    AdvanceTo,
    Allowed,
    ApproveCards,
    EnterCode,
    Fault,
    KillRestart,
    Say,
    Scenario,
    Sim,
    Step,
    WaitForStatus,
)
from evals.runner.usage import read_calls, summarize

from wg.contracts.fixture import Fixture
from wg.k6.fixture import read_profile

SCHEMA_VERSION = 1
K7_EVENT = "explanation_replaced"
APPROVAL_ROUNDS = 5
CODE_ROUNDS = 3


class EpisodeAbort(RuntimeError):
    """The episode cannot go on: the backend is unreachable or a setup call failed."""


@dataclass
class EpisodeSettings:
    """What the run decides for every episode."""

    env_file: Path | None
    force_stub: bool
    spend_cap_cny: str | None
    episode_timeout_s: float


@dataclass
class Recorder:
    """Everything an episode writes into its JSON file."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)
    codes: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    allow: list[Allowed] = field(default_factory=list)
    # Cards that were confirmed or are no longer open; a declined card is judged again later.
    handled_cards: set[str] = field(default_factory=set)
    requested: set[str] = field(default_factory=set)
    verified: set[str] = field(default_factory=set)
    clock: dict[str, Any] | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"text": response.text[:500]}


class Episode:
    def __init__(
        self,
        scenario: Scenario,
        system: str,
        repeat: int,
        out_dir: Path,
        settings: EpisodeSettings,
    ) -> None:
        self.scenario = scenario
        self.system = system
        self.repeat = repeat
        self.episode_id = f"{scenario.id}.{system}.r{repeat}"
        self.out_dir = out_dir
        self.settings = settings
        self.workdir = out_dir / "work" / self.episode_id
        self.rec = Recorder()
        self.backend: Backend | None = None
        self.customer: Session | None = None
        self.demo: Session | None = None
        self.db = DbView(self.workdir / "wg.db")
        self.deadline = time.monotonic() + settings.episode_timeout_s
        profile: Fixture = read_profile(scenario.profile, scenario.variant)
        self.fixture = profile
        self.person = next(c for c in profile.customers if c.id == scenario.customer)
        self.demo_user = next(o for o in profile.operators if o.role.value == "demo")

    # Lifecycle

    def run(self, cap_cny: str | None) -> dict[str, Any]:
        started = _now()
        began = time.monotonic()
        status = "completed"
        try:
            self._setup(cap_cny)
            for index, step in enumerate(self.scenario.steps):
                if time.monotonic() > self.deadline:
                    raise EpisodeAbort("episode timeout")
                self._run_step(index, step)
        except (EpisodeAbort, BackendError, httpx.HTTPError) as error:
            status = "aborted"
            self.rec.errors.append(f"{type(error).__name__}: {error}")
        except Exception:
            status = "aborted"
            self.rec.errors.append(traceback.format_exc(limit=6))
        finally:
            collected = self._collect()
        return {
            "schema_version": SCHEMA_VERSION,
            "episode_id": self.episode_id,
            "scenario_id": self.scenario.id,
            "case": self.scenario.case,
            "category": self.scenario.category,
            "system": self.system,
            "repeat": self.repeat,
            "customer_id": self.scenario.customer,
            "profile": self.scenario.profile,
            "variant": self.scenario.variant,
            "expectations": self.scenario.expectations,
            "status": status,
            "started_at": started,
            "finished_at": _now(),
            "duration_s": round(time.monotonic() - began, 3),
            "backend_starts": self.backend.starts if self.backend else 0,
            "steps": self.rec.steps,
            "turns": self.rec.turns,
            "dispositions": [turn["disposition"] for turn in self.rec.turns],
            "cards": self.rec.cards,
            "codes": self.rec.codes,
            "k7_replacements": [
                turn["index"] for turn in self.rec.turns if turn["explanation_replaced"]
            ],
            "errors": self.rec.errors,
            **collected,
        }

    def _setup(self, cap_cny: str | None) -> None:
        port = free_port()
        env = backend_environment(
            self.workdir,
            port,
            env_file=self.settings.env_file,
            stub_script=self.scenario.stub_script,
            force_stub=self.settings.force_stub,
            spend_cap_cny=cap_cny or self.settings.spend_cap_cny,
        )
        self.backend = Backend(self.workdir, env)
        self.backend.start()
        self._connect()
        response = self._demo.post(
            "/sim/reset", {"profile": self.scenario.profile, "variant": self.scenario.variant}
        )
        self._require(response, "reset")
        self.rec.clock = _body(response).get("clock")
        self._login_customer()

    @property
    def _demo(self) -> Session:
        assert self.demo is not None
        return self.demo

    @property
    def _customer(self) -> Session:
        assert self.customer is not None
        return self.customer

    def _connect(self) -> None:
        """New HTTP sessions for the backend that is running now; the demo panel logs in."""
        assert self.backend is not None
        for old in (self.customer, self.demo):
            if old is not None:
                old.close()
        self.customer = Session(self.backend.base_url)
        self.demo = Session(self.backend.base_url)
        self.demo.login(self.demo_user.handle, self.demo_user.password)

    def _login_customer(self) -> None:
        self._customer.login(self.person.handle, self.person.password)
        if self.system == "flat":
            self._require(self._demo.post("/sim/agent-mode", {"mode": "flat"}), "agent mode")

    @staticmethod
    def _require(response: httpx.Response, what: str) -> None:
        if response.status_code >= 400:
            raise EpisodeAbort(f"{what}: {response.status_code} {response.text[:300]}")

    # Steps

    def _run_step(self, index: int, step: Step) -> None:
        body = step.body
        record: dict[str, Any] = {"index": index, "kind": step.kind, "at": _now(), "ok": True}
        self.rec.steps.append(record)
        if isinstance(body, Say):
            self._say(index, body, record)
        elif isinstance(body, ApproveCards):
            self._approve_cards(body, record)
        elif isinstance(body, EnterCode):
            self._enter_code()
        elif isinstance(body, AdvanceTo):
            self._advance(body, record)
        elif isinstance(body, KillRestart):
            self._kill_restart(body, record)
        elif isinstance(body, Fault):
            self._fault(body, record)
        elif isinstance(body, Sim):
            self._sim(body, record)
        elif isinstance(body, WaitForStatus):
            self._wait_for_status(body, record)
        else:  # pragma: no cover - the schema lists every step kind
            raise EpisodeAbort(f"unhandled step {step.kind}")

    def _say(self, index: int, step: Say, record: dict[str, Any]) -> None:
        calls_before = len(read_calls(self.workdir / "llm_calls.jsonl"))
        mark = self.db.audit_mark()
        began = time.monotonic()
        response = self._customer.post(
            "/v1/chat/messages", {"text": step.text}, timeout=CHAT_TIMEOUT_S
        )
        latency_ms = round((time.monotonic() - began) * 1000)
        body = _body(response)
        calls = read_calls(self.workdir / "llm_calls.jsonl")[calls_before:]
        ok = response.status_code == 200
        record["ok"] = ok
        record["http_status"] = response.status_code
        audit = self.db.audit_since(mark)
        self.rec.turns.append(
            {
                "index": len(self.rec.turns),
                "step": index,
                "customer_text": step.text,
                "http_status": response.status_code,
                "reply": body if ok else None,
                "error": None if ok else body,
                "disposition": body.get("disposition") if ok else None,
                "explanation_replaced": bool(ok and body.get("explanation_replaced")),
                "latency_ms": latency_ms,
                "model": summarize(calls, brief=True),
                # The API does not return tool names; refusals and decisions are in the audit rows.
                "tool_events": audit,
                "k7_events": [row for row in audit if row["event"] == K7_EVENT],
            }
        )

    def _approve_cards(self, step: ApproveCards, record: dict[str, Any]) -> None:
        self.rec.allow.extend(step.allow)
        allow = list(step.allow)
        seen: set[str] = set()
        rounds = 0
        for _ in range(APPROVAL_ROUNDS):
            progress = False
            for card in self.db.open_cards(self.scenario.customer):
                if card["card_id"] in self.rec.handled_cards or card["card_id"] in seen:
                    continue
                seen.add(card["card_id"])
                progress |= self._decide_card(card, allow)
            if not progress:
                break
            rounds += 1
        record["rounds"] = rounds

    def _decide_card(self, card: dict[str, Any], allow: list[Allowed]) -> bool:
        terms = card["terms"]
        entry = {"card_id": card["card_id"], "terms": policy.describe_terms(terms)}
        self.rec.cards.append(entry)
        shown = self._customer.get(f"/v1/cards/{card['card_id']}")
        view = _body(shown)
        if shown.status_code != 200 or view.get("status") != "open":
            # An expired or superseded card is never offered again.
            self.rec.handled_cards.add(card["card_id"])
            entry.update(decision="skipped", reason=f"card not open ({shown.status_code})")
            return False
        if view["terms_hash"] != card["terms_hash"]:
            entry.update(decision="declined", reason="terms hash differs from the stored terms")
            return False
        if not policy.card_allowed(terms, allow):
            entry.update(decision="declined", reason="not in the allow-list")
            return False
        self.rec.handled_cards.add(card["card_id"])
        response = self._customer.post(
            f"/v1/cards/{card['card_id']}/confirm", {"terms_hash": view["terms_hash"]}
        )
        result = _body(response)
        entry.update(decision="approved", http_status=response.status_code)
        if response.status_code < 300:
            challenge = result.get("challenge") or {}
            entry.update(
                action_status=result.get("action_status"),
                challenge_id=challenge.get("challenge_id"),
                operation_id=(result.get("operation") or {}).get("operation_id"),
            )
        else:
            entry["error"] = result
        return True

    def _enter_code(self) -> None:
        for _ in range(CODE_ROUNDS):
            entered = self._verify_open_codes()
            requested = self._request_missing_challenges()
            if not entered and not requested:
                break

    def _verify_open_codes(self) -> int:
        sms = self._customer.get("/v1/sms")
        if sms.status_code != 200:
            self.rec.errors.append(f"GET /v1/sms -> {sms.status_code}")
            return 0
        entered = 0
        for message in _body(sms).get("messages", []):
            challenge_id = message["challenge_id"]
            if challenge_id in self.rec.verified:
                continue
            action = self.db.action_params(message["logical_action_id"])
            entry: dict[str, Any] = {
                "challenge_id": challenge_id,
                "logical_action_id": message["logical_action_id"],
                "kind": message["kind"],
                "amount_fen": message.get("amount_fen"),
            }
            self.rec.codes.append(entry)
            if action is None or not policy.code_allowed(
                action["kind"], action["params"], self.rec.allow
            ):
                entry["decision"] = "declined"
                continue
            self.rec.verified.add(challenge_id)
            response = self._customer.post(
                f"/v1/challenges/{challenge_id}/verify", {"code": message["code"]}
            )
            result = _body(response)
            entry.update(decision="entered", http_status=response.status_code)
            if response.status_code < 300:
                entry.update(
                    action_status=result.get("action_status"),
                    operation_id=(result.get("operation") or {}).get("operation_id"),
                )
            else:
                entry["error"] = result
            entered += 1
        return entered

    def _request_missing_challenges(self) -> int:
        """Ask for a code for an allowed action that waits for one and has none open."""
        sms = _body(self._customer.get("/v1/sms")).get("messages", [])
        have = {message["logical_action_id"] for message in sms}
        requested = 0
        for action_id in self.db.actions_awaiting_stepup(self.scenario.customer):
            if action_id in have or action_id in self.rec.requested:
                continue
            action = self.db.action_params(action_id)
            if action is None or not policy.code_allowed(
                action["kind"], action["params"], self.rec.allow
            ):
                continue
            self.rec.requested.add(action_id)
            response = self._customer.post(f"/v1/actions/{action_id}/challenge")
            self.rec.codes.append(
                {
                    "logical_action_id": action_id,
                    "decision": "requested",
                    "http_status": response.status_code,
                }
            )
            requested += int(response.status_code < 300)
        return requested

    def _advance(self, step: AdvanceTo, record: dict[str, Any]) -> None:
        if step.to is not None:
            body: dict[str, Any] = {"to": step.to}
        else:
            body = {"days": step.days, "minutes": step.minutes}
        response = self._demo.post("/sim/clock/advance", body)
        record["ok"] = response.status_code == 200
        record["http_status"] = response.status_code
        record["clock"] = _body(response)
        self.rec.clock = record["clock"] if record["ok"] else self.rec.clock

    def _fault(self, step: Fault, record: dict[str, Any]) -> None:
        response = self._demo.post(
            "/sim/faults",
            {"fault": step.fault, "count": step.count, "persistent": step.persistent},
        )
        record["ok"] = response.status_code == 200
        record["http_status"] = response.status_code
        if not record["ok"]:
            record["error"] = _body(response)

    def _sim(self, step: Sim, record: dict[str, Any]) -> None:
        response = self._demo.post(step.post, step.body)
        record["route"] = step.post
        record["ok"] = response.status_code < 300
        record["http_status"] = response.status_code
        record["response"] = _body(response)
        if not record["ok"]:
            self.rec.errors.append(f"POST {step.post} -> {response.status_code}")

    def _kill_restart(self, step: KillRestart, record: dict[str, Any]) -> None:
        assert self.backend is not None
        if step.how == "crash" and self.backend.running:
            try:
                self._demo.post("/sim/crash")
            except httpx.TransportError:
                pass  # the process ends before it answers
            self.backend.wait_exit()
        else:
            self.backend.kill()
        self.backend.start()
        self._connect()
        self._login_customer()
        record["backend_starts"] = self.backend.starts

    def _wait_for_status(self, step: WaitForStatus, record: dict[str, Any]) -> None:
        wanted = set(step.status)
        deadline = min(time.monotonic() + step.timeout_s, self.deadline)
        seen: str | None = None
        while True:
            if step.plan is not None:
                seen = self.db.newest_plan_status(self.scenario.customer)
            else:
                assert step.action_kind is not None
                seen = self.db.newest_action_status(self.scenario.customer, step.action_kind)
            if seen in wanted:
                break
            if time.monotonic() >= deadline:
                record["ok"] = False
                record["timeout"] = True
                break
            time.sleep(step.poll_s)
        record["status_seen"] = seen

    # Collection

    def _collect(self) -> dict[str, Any]:
        """Receipts, model usage and the database copy; the backend is stopped on the way."""
        receipts: list[dict[str, Any]] = []
        if self.backend is not None and self.backend.running and self.customer is not None:
            try:
                for operation_id in self.db.operation_ids(self.scenario.customer):
                    response = self._customer.get(f"/v1/receipts/{operation_id}")
                    receipts.append(
                        {
                            "operation_id": operation_id,
                            "http_status": response.status_code,
                            "receipt": _body(response) if response.status_code == 200 else None,
                        }
                    )
            except (httpx.HTTPError, OSError) as error:
                self.rec.errors.append(f"receipts: {type(error).__name__}: {error}")
        for session in (self.customer, self.demo):
            if session is not None:
                session.close()
        if self.backend is not None:
            self.backend.stop()
        episodes = self.out_dir / "episodes"
        episodes.mkdir(parents=True, exist_ok=True)
        calls_path = self.workdir / "llm_calls.jsonl"
        copy_file(calls_path, episodes / f"{self.episode_id}.llm_calls.jsonl")
        if self.db.path.is_file():
            copy_database(self.db.path, episodes / f"{self.episode_id}.db")
        model = summarize(read_calls(calls_path))
        for leftover in self.workdir.glob("wg.db*"):
            leftover.unlink()
        calls_path.unlink(missing_ok=True)
        return {
            "receipts": receipts,
            "model": model,
            "files": {
                "database": f"{self.episode_id}.db",
                "model_calls": f"{self.episode_id}.llm_calls.jsonl",
                "server_log": f"../work/{self.episode_id}/server.log",
            },
            "final_clock": self.rec.clock,
        }
