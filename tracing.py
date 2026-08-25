from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from schemas import ConversationState
from workflow import (
    OrderLookup,
    RefundCreator,
    ExchangeCreator,
    Interpreter,
    KnowledgeAnswerer,
    KnowledgeSearch,
    process_message,
)

TraceEventType = Literal[
    "interpreter_call",
    "tool_call",
    "state_transition",
]


class TraceEvent(BaseModel):
    event_type: TraceEventType
    component: str
    outcome: str


class TurnTrace(BaseModel):
    turn_id: str
    started_at: str
    duration_ms: int
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    intent: str | None = None
    success: bool = False
    error_type: str | None = None
    events: list[TraceEvent] = Field(default_factory=list)


OrderIdPattern = re.compile(r"\b([A-Z]\d{4})\b", re.IGNORECASE)
RequestIdPattern = re.compile(r"\b(?:EX|RF)-\d{4}\b", re.IGNORECASE)


def _redact_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = OrderIdPattern.sub("[ORDER_ID_REDACTED]", value)
    return RequestIdPattern.sub("[REQUEST_ID_REDACTED]", value)


def _sanitize_state(state: ConversationState) -> dict[str, Any]:
    intent = state.intent_result
    return {
        "intent_result": {
            "intent": intent.intent if intent is not None else None,
            "missing_information": intent.missing_information if intent else [],
        },
        "status": state.status,
        "assistant_message": _redact_text(state.assistant_message),
        "pending_action": state.pending_action,
        "has_order": state.order is not None,
        "has_exchange_request": state.exchange_request is not None,
        "has_refund_request": state.refund_request is not None,
        "citation_count": len(state.knowledge_citations),
    }


def _default_trace_sink(trace: TurnTrace) -> None:
    logging.getLogger("uvicorn.error").info(
        "agent_trace=%s",
        json.dumps(trace.model_dump(exclude_none=True), ensure_ascii=False),
    )

def run_traced_message(
    state: ConversationState,
    user_message: str,
    interpreter: Interpreter,
    order_lookup: OrderLookup | None = None,
    exchange_creator: ExchangeCreator | None = None,
    refund_creator: RefundCreator | None = None,
    trace_sink: Callable[[TurnTrace], None] | None = None,
    knowledge_search: KnowledgeSearch | None = None,
    knowledge_answerer: KnowledgeAnswerer | None = None,
) -> tuple[ConversationState, TurnTrace]:
    """Run one turn and return the next state together with a sanitized trace."""
    events: list[TraceEvent] = []
    started_at = datetime.now(timezone.utc)
    start = perf_counter()

    state_before = _sanitize_state(state)
    state_after = state_before
    state_before_intent = state.intent_result.intent if state.intent_result else None

    wrapped_interpreter = interpreter
    wrapped_order_lookup = order_lookup
    wrapped_exchange_creator = exchange_creator
    wrapped_refund_creator = refund_creator
    wrapped_knowledge_search = knowledge_search
    wrapped_knowledge_answerer = knowledge_answerer

    if knowledge_search is not None:
        def _search(query: str, top_k: int) -> Any:
            try:
                result = knowledge_search(query, top_k)
            except Exception as exc:
                events.append(TraceEvent(event_type="tool_call", component="knowledge_search", outcome=f"error:{type(exc).__name__}"))
                raise
            events.append(TraceEvent(event_type="tool_call", component="knowledge_search", outcome=f"success:{len(result)}"))
            return result
        wrapped_knowledge_search = _search

    if knowledge_answerer is not None:
        def _answer(query: str, hits: Any) -> Any:
            try:
                result = knowledge_answerer(query, hits)
            except Exception as exc:
                events.append(TraceEvent(event_type="tool_call", component="knowledge_answerer", outcome=f"error:{type(exc).__name__}"))
                raise
            events.append(TraceEvent(event_type="tool_call", component="knowledge_answerer", outcome="success"))
            return result
        wrapped_knowledge_answerer = _answer

    if interpreter is not None:
        def _interp(msg: str) -> Any:
            try:
                result = interpreter(msg)
            except Exception as exc:
                events.append(
                    TraceEvent(
                        event_type="interpreter_call",
                        component="interpreter",
                        outcome=f"error:{type(exc).__name__}",
                    )
                )
                raise
            else:
                events.append(
                    TraceEvent(
                        event_type="interpreter_call",
                        component="interpreter",
                        outcome="success",
                    )
                )
                return result

        wrapped_interpreter = _interp

    if order_lookup is not None:
        def _lookup(order_id: str) -> Any:
            try:
                result = order_lookup(order_id)
            except Exception as exc:
                outcome = getattr(exc, "trace_outcome", "upstream_error")
                if outcome not in {
                    "success",
                    "not_found",
                    "timeout",
                    "upstream_error",
                    "circuit_open",
                }:
                    outcome = "upstream_error"
                events.append(
                    TraceEvent(
                        event_type="tool_call",
                        component="order_lookup",
                        outcome=outcome,
                    )
                )
                raise
            else:
                events.append(
                    TraceEvent(
                        event_type="tool_call",
                        component="order_lookup",
                        outcome="success" if result is not None else "not_found",
                    )
                )
                return result

        wrapped_order_lookup = _lookup

    if exchange_creator is not None:
        def _create_exchange(order_id: str, reason: str) -> Any:
            try:
                result = exchange_creator(order_id, reason)
            except Exception as exc:
                events.append(
                    TraceEvent(
                        event_type="tool_call",
                        component="exchange_creator",
                        outcome=f"error:{type(exc).__name__}",
                    )
                )
                raise
            else:
                events.append(
                    TraceEvent(
                        event_type="tool_call",
                        component="exchange_creator",
                        outcome="success",
                    )
                )
                return result

        wrapped_exchange_creator = _create_exchange

    if refund_creator is not None:
        def _create_refund(order_id: str, reason: str) -> Any:
            try:
                result = refund_creator(order_id, reason)
            except Exception as exc:
                events.append(
                    TraceEvent(
                        event_type="tool_call",
                        component="refund_creator",
                        outcome=f"error:{type(exc).__name__}",
                    )
                )
                raise
            else:
                events.append(
                    TraceEvent(
                        event_type="tool_call",
                        component="refund_creator",
                        outcome="success",
                    )
                )
                return result

        wrapped_refund_creator = _create_refund

    next_state = state
    success = True
    error_type: str | None = None
    try:
        next_state = process_message(
            state,
            user_message,
            wrapped_interpreter,
            wrapped_order_lookup,
            wrapped_exchange_creator,
            wrapped_refund_creator,
            wrapped_knowledge_search,
            wrapped_knowledge_answerer,
        )
    except Exception as exc:
        success = False
        error_type = type(exc).__name__
        state_after = state_before
        raise
    finally:
        if success:
            sanitized_after = _sanitize_state(next_state)
        else:
            sanitized_after = state_after
        before_status = state_before.get("status")
        after_status = sanitized_after.get("status")
        events.append(
            TraceEvent(
                event_type="state_transition",
                component="workflow",
                outcome=f"{before_status}->{after_status}",
            )
        )

        turn_trace = TurnTrace(
            turn_id=uuid4().hex,
            started_at=started_at.isoformat(),
            duration_ms=int((perf_counter() - start) * 1000),
            state_before=state_before,
            state_after=sanitized_after,
            intent=next_state.intent_result.intent
            if next_state.intent_result
            else state_before_intent,
            success=success,
            error_type=error_type,
            events=events,
        )

        sink = trace_sink if trace_sink is not None else _default_trace_sink
        try:
            if sink is not None:
                sink(turn_trace)
        except Exception:
            logging.getLogger(__name__).exception("trace sink failed; ignored")

    return next_state, turn_trace
