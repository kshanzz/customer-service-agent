from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, StringConstraints

from exchange_tools import ExchangeRequest, InMemoryExchangeService
from interpreter import interpret_intent
from order_tools import OrderRecord, query_order
from refund_tools import InMemoryRefundService, RefundRequest
from schemas import ConversationState, IntentResult
from session_store import InMemorySessionStore, SessionNotFoundError
from workflow import process_message


Interpreter = Callable[[str], IntentResult]
OrderLookup = Callable[[str], OrderRecord | None]
ExchangeCreator = Callable[[str, str], ExchangeRequest]
RefundCreator = Callable[[str, str], RefundRequest]

PublicStatus = Literal[
    "new",
    "waiting_for_information",
    "ready",
    "order_checked",
    "waiting_for_confirmation",
    "completed",
    "cancelled",
    "rejected",
    "answered",
]
PublicIntent = Literal[
    "exchange",
    "refund",
    "logistics",
    "complaint",
    "unknown",
]

TERMINAL_STATUSES = {
    "ready",
    "order_checked",
    "completed",
    "cancelled",
    "rejected",
    "answered",
}


class MessageRequest(BaseModel):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SessionResponse(BaseModel):
    session_id: str
    status: PublicStatus
    assistant_message: str | None
    intent: PublicIntent | None
    order_id: str | None
    pending_action: Literal["exchange", "refund"] | None
    request_id: str | None


def _public_snapshot(
    session_id: str,
    state: ConversationState,
) -> SessionResponse:
    request = state.exchange_request or state.refund_request
    return SessionResponse(
        session_id=session_id,
        status=state.status,
        assistant_message=state.assistant_message,
        intent=(state.intent_result.intent if state.intent_result else None),
        order_id=(state.intent_result.order_id if state.intent_result else None),
        pending_action=state.pending_action,
        request_id=request.request_id if request else None,
    )


def create_app(
    interpreter: Interpreter | None = None,
    order_lookup: OrderLookup | None = None,
    exchange_creator: ExchangeCreator | None = None,
    refund_creator: RefundCreator | None = None,
    session_store: InMemorySessionStore | None = None,
) -> FastAPI:
    """Create an API with isolated app-level services and injectable tools."""
    resolved_interpreter = interpreter if interpreter is not None else interpret_intent
    resolved_order_lookup = order_lookup if order_lookup is not None else query_order

    if exchange_creator is None:
        exchange_service = InMemoryExchangeService()
        resolved_exchange_creator = exchange_service.create_request
    else:
        resolved_exchange_creator = exchange_creator

    if refund_creator is None:
        refund_service = InMemoryRefundService()
        resolved_refund_creator = refund_service.create_request
    else:
        resolved_refund_creator = refund_creator

    resolved_session_store = (
        session_store if session_store is not None else InMemorySessionStore()
    )
    app = FastAPI(
        title="Customer Service Agent API",
        description=(
            "Uses in-memory sessions for a single-process demonstration only."
        ),
    )

    def get_state(session_id: str) -> ConversationState:
        try:
            return resolved_session_store.get(session_id)
        except SessionNotFoundError:
            raise HTTPException(status_code=404, detail="Session not found") from None

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/sessions", response_model=SessionResponse)
    async def create_session() -> SessionResponse:
        session_id = resolved_session_store.create()
        return _public_snapshot(session_id, resolved_session_store.get(session_id))

    @app.get("/sessions/{session_id}", response_model=SessionResponse)
    async def get_session(session_id: str) -> SessionResponse:
        return _public_snapshot(session_id, get_state(session_id))

    @app.post(
        "/sessions/{session_id}/messages",
        response_model=SessionResponse,
    )
    async def send_message(
        session_id: str,
        request: MessageRequest,
    ) -> SessionResponse:
        state = get_state(session_id)
        if state.status in TERMINAL_STATUSES:
            return _public_snapshot(session_id, state)

        try:
            next_state = process_message(
                state,
                request.message,
                resolved_interpreter,
                resolved_order_lookup,
                resolved_exchange_creator,
                resolved_refund_creator,
            )
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="Customer service processing is temporarily unavailable",
            ) from None

        resolved_session_store.save(session_id, next_state)
        return _public_snapshot(session_id, next_state)

    return app
