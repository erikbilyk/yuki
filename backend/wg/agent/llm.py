"""Model adapter: one audited, budget-limited path to the language model (SPEC §18.1).

Providers return a `ModelReply`; `ModelAdapter` wraps one provider with the checks every
call needs: never inside a unit of work, spend guard, one metadata record per call.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

from wg.contracts.stub_script import StubScript, StubTextTurn
from wg.k6.db import in_transaction
from wg.settings import Settings

TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
RETRY_DELAY_S = 0.5
ERROR_BODY_LIMIT = 500
COST_PLACES = Decimal("0.000001")
TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)


@dataclass(frozen=True)
class Usage:
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0


class LlmError(Exception):
    """A model call that produced no usable reply; `reason` is the short code in the record.

    `usage` and `returned_model` are set when the provider answered and billed the call.
    """

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.usage = Usage()
        self.returned_model: str | None = None


class SpendCapReached(LlmError):
    """The recorded spend is at or above WG_LLM_SPEND_CAP_CNY; no call was made."""

    def __init__(self) -> None:
        super().__init__("spend_cap", "recorded model spend reached the cap")


class ProviderError(LlmError):
    """The provider refused the request or the network failed."""

    def __init__(self, reason: str, status: int | None = None, body: str = "") -> None:
        super().__init__(reason, f"{reason} {body}".strip())
        self.status = status
        self.body = body


class ModelOutputError(LlmError):
    """The model returned something the loop must not act on, such as non-JSON arguments."""


class StubScriptExhausted(LlmError):
    def __init__(self) -> None:
        super().__init__("stub_exhausted", "the stub script has no turn left")


@dataclass(frozen=True)
class ToolCallRequest:
    """A tool call as sent: `arguments_json` is the raw string, `arguments` its parse."""

    call_id: str
    name: str
    arguments_json: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelReply:
    text: str
    tool_calls: tuple[ToolCallRequest, ...] = ()
    usage: Usage = field(default_factory=Usage)
    returned_model: str = ""
    reasoning: str = ""


class ChatProvider(Protocol):
    name: str

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelReply: ...


@dataclass(frozen=True)
class PriceTable:
    """CNY per million tokens: cache hit, cache miss, output."""

    hit: Decimal
    miss: Decimal
    out: Decimal

    @classmethod
    def parse(cls, text: str) -> PriceTable:
        message = "WG_LLM_PRICE_TABLE must be hit/miss/out in CNY per 1M tokens"
        try:
            hit, miss, out = (Decimal(part) for part in text.split("/"))
        except (ArithmeticError, ValueError):
            raise ValueError(message) from None
        if min(hit, miss, out) < 0:
            raise ValueError(message)
        return cls(hit, miss, out)

    def cost(self, usage: Usage) -> Decimal:
        total = (
            usage.cache_hit_tokens * self.hit
            + usage.cache_miss_tokens * self.miss
            + usage.completion_tokens * self.out
        )
        return (total / TOKENS_PER_PRICE_UNIT).quantize(COST_PLACES, rounding=ROUND_UP)


def tool_schema_version(tools: Sequence[dict[str, Any]]) -> str:
    """A short digest of the tool definitions sent with a call."""
    canonical = json.dumps(tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def parse_tool_calls(raw_calls: Sequence[dict[str, Any]]) -> tuple[ToolCallRequest, ...]:
    """Tool calls from a chat-completions message; arguments that are not a JSON object fail."""
    calls = []
    for index, raw in enumerate(raw_calls):
        function = raw.get("function") or {}
        name = function.get("name")
        arguments_json = function.get("arguments")
        if not isinstance(name, str) or not name:
            raise ModelOutputError("invalid_tool_call", "a tool call has no name")
        if not isinstance(arguments_json, str):
            raise ModelOutputError("invalid_tool_arguments", f"{name}: arguments are not text")
        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError:
            raise ModelOutputError(
                "invalid_tool_arguments", f"{name}: arguments are not valid JSON"
            ) from None
        if not isinstance(arguments, dict):
            raise ModelOutputError(
                "invalid_tool_arguments", f"{name}: arguments are not a JSON object"
            )
        call_id = raw.get("id")
        calls.append(
            ToolCallRequest(
                call_id=call_id if isinstance(call_id, str) and call_id else f"call_{index}",
                name=name,
                arguments_json=arguments_json or "{}",
                arguments=arguments,
            )
        )
    return tuple(calls)


class DeepSeekProvider:
    """OpenAI-compatible chat completions over httpx; the environment's proxy is ignored."""

    name = "deepseek"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_s,
            trust_env=False,
            proxy=settings.outbound_proxy or None,
            transport=transport,
        )

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelReply:
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": list(messages),
            "thinking": {"type": self.settings.llm_thinking},
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        try:
            body = self._post(payload)
        except httpx.TimeoutException:
            raise ProviderError("timeout") from None
        except httpx.TransportError:
            raise ProviderError("network") from None
        return self._reply(body)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        for attempt in (1, 2):
            try:
                response = self._client.post("/chat/completions", json=payload, headers=headers)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                self._sleep(RETRY_DELAY_S)
                continue
            if response.status_code in TRANSIENT_STATUS and attempt == 1:
                self._sleep(RETRY_DELAY_S)
                continue
            if response.status_code != 200:
                raise ProviderError(
                    f"http_{response.status_code}",
                    response.status_code,
                    self._scrub(response.text[:ERROR_BODY_LIMIT]),
                )
            try:
                body = response.json()
            except ValueError:
                raise ProviderError("bad_response", response.status_code) from None
            if not isinstance(body, dict):
                raise ProviderError("bad_response", response.status_code)
            return body
        raise AssertionError("unreachable")

    def _scrub(self, text: str) -> str:
        key = self.settings.llm_api_key
        return text.replace(key, "[key]") if key else text

    def _reply(self, body: dict[str, Any]) -> ModelReply:
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("bad_response") from None
        usage = body.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = usage.get("prompt_cache_miss_tokens")
        details = usage.get("completion_tokens_details") or {}
        counted = Usage(
            cache_hit_tokens=hit,
            cache_miss_tokens=int(miss) if miss is not None else max(prompt_tokens - hit, 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
        )
        returned_model = str(body.get("model") or "")
        try:
            tool_calls = parse_tool_calls(message.get("tool_calls") or [])
        except LlmError as error:
            error.usage = counted
            error.returned_model = returned_model
            raise
        return ModelReply(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            usage=counted,
            returned_model=returned_model,
            reasoning=message.get("reasoning_content") or "",
        )


class StubProvider:
    """Replays the turns of a stub script in order, one per call (SPEC §18.1)."""

    name = "stub"

    def __init__(self, script_path: str | Path) -> None:
        loaded = yaml.safe_load(Path(script_path).read_text(encoding="utf-8"))
        self._turns = StubScript.model_validate(loaded or []).root
        self._next = 0

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelReply:
        if self._next >= len(self._turns):
            raise StubScriptExhausted
        turn = self._turns[self._next]
        self._next += 1
        if isinstance(turn, StubTextTurn):
            return ModelReply(text=turn.text, returned_model="stub")
        calls = tuple(
            ToolCallRequest(
                call_id=f"stub_{self._next}_{index}",
                name=call.name,
                arguments_json=json.dumps(call.arguments, ensure_ascii=False),
                arguments=dict(call.arguments),
            )
            for index, call in enumerate(turn.tool_calls)
        )
        return ModelReply(text="", tool_calls=calls, returned_model="stub")


class ModelAdapter:
    """The single entry point for model calls."""

    def __init__(
        self,
        provider: ChatProvider,
        settings: Settings,
        *,
        log_path: Path,
        build_id: str = "unknown",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.log_path = log_path
        self.build_id = build_id
        self._prices = PriceTable.parse(settings.llm_price_table)
        self._clock = clock
        self._lock = threading.Lock()

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        *,
        prompt_version: str,
    ) -> ModelReply:
        if in_transaction():
            raise RuntimeError("a model call must not run inside a unit of work")
        started = time.monotonic()
        record = {
            "at": self._clock().isoformat(timespec="milliseconds"),
            "provider": self.provider.name,
            "requested_model": self.settings.llm_model,
            "returned_model": None,
            "thinking": self.settings.llm_thinking,
            "prompt_version": prompt_version,
            "tool_schema_version": tool_schema_version(tools),
            "build_id": self.build_id,
        }
        reply: ModelReply | None = None
        error: LlmError | None = None
        if self.recorded_spend() >= self.settings.llm_spend_cap_cny:
            error = SpendCapReached()
        else:
            try:
                reply = self.provider.complete(messages, tools)
            except LlmError as caught:
                error = caught
        usage = reply.usage if reply else error.usage if error else Usage()
        returned_model = reply.returned_model if reply else error.returned_model if error else None
        record.update(
            returned_model=returned_model,
            latency_ms=round((time.monotonic() - started) * 1000),
            usage={
                "cache_hit_tokens": usage.cache_hit_tokens,
                "cache_miss_tokens": usage.cache_miss_tokens,
                "completion_tokens": usage.completion_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
            },
            cost_cny=str(self._prices.cost(usage)),
            error=error.reason if error else None,
        )
        self._append(record)
        if error:
            raise error
        assert reply is not None
        return reply

    def recorded_spend(self) -> Decimal:
        """Estimated CNY spent so far, summed from the call records."""
        total = Decimal(0)
        with self._lock:
            if not self.log_path.is_file():
                return total
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            try:
                total += Decimal(json.loads(line)["cost_cny"])
            except (ValueError, KeyError, TypeError, ArithmeticError):
                continue
        return total

    def _append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def call_log_path(settings: Settings) -> Path:
    """`llm_calls.jsonl` next to the working copy's database (`var/` by default)."""
    return settings.db_path.parent / "llm_calls.jsonl"


def build_adapter(
    settings: Settings,
    *,
    log_path: Path | None = None,
    build_id: str = "unknown",
    transport: httpx.BaseTransport | None = None,
) -> ModelAdapter:
    """The adapter for WG_LLM_PROVIDER: `deepseek` over HTTP or `stub` from a script file."""
    provider: ChatProvider
    if settings.llm_provider == "stub":
        if not settings.llm_stub_script:
            raise LlmError("stub_script_missing", "WG_LLM_STUB_SCRIPT is empty")
        provider = StubProvider(settings.llm_stub_script)
    else:
        if not settings.llm_api_key:
            raise LlmError("api_key_missing", "WG_LLM_API_KEY is empty")
        provider = DeepSeekProvider(settings, transport=transport)
    return ModelAdapter(
        provider, settings, log_path=log_path or call_log_path(settings), build_id=build_id
    )
