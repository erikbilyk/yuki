"""The chat route's turn: stored masked history in, checked reply out (SPEC §17, §18.4, §18.6).

Only masked text is stored. The customer's message is stored before the model is called, so
it stays in the history when the model fails; the reply is stored once K7 has checked it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from pydantic import BaseModel

from wg.agent.llm import LlmError, ModelAdapter
from wg.agent.registry import ToolOutcome, ToolRegistry
from wg.agent.turn import AgentTurn, agent_turn
from wg.contracts.api import (
    ApprovalRequestResult,
    CancelResult,
    ChatHistoryView,
    ChatMessageView,
    ChatReply,
    ChatRole,
    DraftResult,
    PlanDraftResult,
)
from wg.contracts.enums import Channel, Disposition, PlanMode
from wg.contracts.errors import ErrorCode, WgError
from wg.k6 import chat
from wg.k6.sessions import SessionRow

# Earlier messages of the session given to the model with each turn.
HISTORY_MESSAGES = 20

_CLARIFY = frozenset({ErrorCode.AMBIGUOUS_PAYEE, ErrorCode.MISSING_SLOT})
_UNSUPPORTED = frozenset({ErrorCode.UNSUPPORTED_REQUEST, ErrorCode.FORBIDDEN_ROUTE})


class ChatService:
    """One customer turn through the agent; the model adapter is built on first use."""

    def __init__(self, registry: ToolRegistry, build_adapter: Callable[[], ModelAdapter]) -> None:
        self.registry = registry
        self.db = registry.db
        self._build_adapter = build_adapter
        self._adapter: ModelAdapter | None = None
        self._lock = threading.Lock()

    def send(self, session: SessionRow, text: str) -> ChatReply:
        """A web turn for the customer of the session."""
        return self.converse(
            session.subject_id,
            session.session_id,
            text,
            channel=Channel.WEB,
            mode=session.agent_mode,
        )

    def converse(
        self,
        customer_id: str,
        conversation_id: str,
        text: str,
        *,
        channel: Channel,
        mode: PlanMode = PlanMode.COORDINATED,
    ) -> ChatReply:
        """One turn of a conversation; its history is the stored messages under the same ID."""
        with self.db.read() as conn:
            earlier = chat.customer_messages(
                conn, customer_id, HISTORY_MESSAGES, session_id=conversation_id
            )
        history = [{"role": m.role.value, "content": m.masked_text} for m in earlier]

        def store_message(masked_text: str) -> None:
            self.db.run_in_uow(
                lambda uow: chat.insert_message(
                    uow,
                    session_id=conversation_id,
                    customer_id=customer_id,
                    channel=channel,
                    role=ChatRole.USER,
                    masked_text=masked_text,
                ),
                "store chat message",
            )

        try:
            done = agent_turn(
                self._adapter_or_refusal(),
                self.registry,
                customer_id=customer_id,
                channel=channel,
                text=text,
                history=history,
                mode=mode,
                on_ingress=store_message,
            )
        except LlmError as error:
            raise WgError(ErrorCode.PROVIDER_TIMEOUT, {"reason": error.reason}) from error
        reply = self._reply(customer_id, done)
        self.db.run_in_uow(
            lambda uow: chat.insert_message(
                uow,
                session_id=conversation_id,
                customer_id=customer_id,
                channel=channel,
                role=ChatRole.ASSISTANT,
                masked_text=shown_text(reply),
                k7_replaced=reply.explanation_replaced,
            ),
            "store chat reply",
        )
        return reply

    def history(self, customer_id: str, limit: int) -> ChatHistoryView:
        """The customer's newest messages on every channel, oldest first."""
        with self.db.read() as conn:
            rows = chat.customer_messages(conn, customer_id, limit)
        return ChatHistoryView(
            messages=tuple(
                ChatMessageView(
                    role=row.role,
                    channel=row.channel,
                    text=row.masked_text,
                    explanation_replaced=row.k7_replaced,
                    created_at=row.created_at,
                )
                for row in rows
            )
        )

    def _adapter_or_refusal(self) -> ModelAdapter:
        with self._lock:
            if self._adapter is None:
                try:
                    self._adapter = self._build_adapter()
                except LlmError as error:
                    raise WgError(ErrorCode.PROVIDER_TIMEOUT, {"reason": error.reason}) from error
            return self._adapter

    def _reply(self, customer_id: str, done: AgentTurn) -> ChatReply:
        outcomes = done.turn.outcomes
        results = [o.result for o in outcomes if o.result is not None]
        plan_id = newest_plan_id(results)
        card_ids = [r.card_id for r in results if isinstance(r, ApprovalRequestResult)]
        if plan_id is not None:
            plan_card = self.registry.services.plans.plan(customer_id, plan_id).card_id
            if plan_card is not None:
                card_ids.append(plan_card)
        reply = done.reply
        return ChatReply(
            disposition=disposition(outcomes),
            facts=reply.facts,
            fact_lines=reply.fact_lines,
            explanation=reply.explanation,
            explanation_replaced=reply.replaced,
            card_ids=tuple(dict.fromkeys(card_ids)),
            logical_action_ids=tuple(dict.fromkeys(_action_ids(results))),
            plan_id=plan_id,
        )


def _action_ids(results: list[BaseModel]) -> list[str]:
    ids: list[str] = []
    for result in results:
        if isinstance(result, DraftResult | ApprovalRequestResult | CancelResult):
            ids.append(result.logical_action_id)
        elif isinstance(result, PlanDraftResult):
            ids.extend(action.logical_action_id for action in result.actions)
    return ids


def newest_plan_id(results: list[BaseModel]) -> str | None:
    """The last plan the turn drafted or composed; a composed plan replaces its sources."""
    plans = [r.plan_id for r in results if isinstance(r, PlanDraftResult) and r.plan_id]
    return plans[-1] if plans else None


def disposition(outcomes: tuple[ToolOutcome, ...] | list[ToolOutcome]) -> Disposition | None:
    """How the turn ended, from its tool calls; None when no call decides it.

    The model never executes anything, so a turn is never `executed`. A refused call in a
    turn that still put a card or a plan before the customer does not make it `unsupported`.
    """
    codes = {o.error.code for o in outcomes if o.error is not None}
    results = [o.result for o in outcomes if o.result is not None]
    if codes & _CLARIFY:
        return Disposition.CLARIFICATION
    if any(isinstance(r, CancelResult) for r in results):
        return Disposition.CANCELLED
    if any(isinstance(r, DraftResult) and not r.feasible for r in results):
        return Disposition.INFEASIBLE
    if codes & _UNSUPPORTED and not any(_prepared(r) for r in results):
        return Disposition.UNSUPPORTED
    return None


def _prepared(result: BaseModel) -> bool:
    if isinstance(result, ApprovalRequestResult):
        return True
    return isinstance(result, PlanDraftResult) and result.plan_id is not None


def shown_text(reply: ChatReply) -> str:
    """The reply as the chat shows it: the fact lines, then the checked explanation."""
    parts = [*reply.fact_lines]
    if reply.explanation:
        parts.append(reply.explanation)
    return "\n".join(parts)
