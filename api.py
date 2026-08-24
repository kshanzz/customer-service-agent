import asyncio
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, StringConstraints

from exchange_tools import ExchangeRequest, InMemoryExchangeService
from interpreter import interpret_intent
from order_tools import OrderRecord, query_order
from refund_tools import InMemoryRefundService, RefundRequest
from schemas import ConversationState, IntentResult, KnowledgeCitation
from session_store import (
    InMemorySessionStore,
    SessionConcurrencyError,
    SessionNotFoundError,
)
from sqlite_store import (
    SQLiteExchangeService,
    SQLiteRefundService,
    SQLiteSessionStore,
)
from tracing import run_traced_message, TurnTrace
from auth import api_key_dependency, resolve_auth_config
from knowledge import KnowledgeHit, KnowledgeSearchService
from grounded_answer import GroundedAnswer, answer_question


Interpreter = Callable[[str], IntentResult]
OrderLookup = Callable[[str], OrderRecord | None]
ExchangeCreator = Callable[[str, str], ExchangeRequest]
RefundCreator = Callable[[str, str], RefundRequest]
TraceSink = Callable[[TurnTrace], None]
KnowledgeSearch = Callable[[str, int], list[KnowledgeHit]]
KnowledgeAnswerer = Callable[[str, list[KnowledgeHit]], GroundedAnswer]

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
    knowledge_citations: list[KnowledgeCitation]


class KnowledgeSearchRequest(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=500)]
    top_k: int = 3


class KnowledgeSearchResponse(BaseModel):
    hits: list[KnowledgeHit]


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
        knowledge_citations=state.knowledge_citations,
    )


def create_app(
    interpreter: Interpreter | None = None,
    order_lookup: OrderLookup | None = None,
    exchange_creator: ExchangeCreator | None = None,
    refund_creator: RefundCreator | None = None,
    session_store: InMemorySessionStore | None = None,
    trace_sink: TraceSink | None = None,
    auth_required: bool | None = None,
    api_key: str | None = None,
    docs_enabled: bool | None = None,
    cors_origins: str | list[str] | None = None,
    knowledge_search: KnowledgeSearch | None = None,
    knowledge_answerer: KnowledgeAnswerer | None = None,
) -> FastAPI:
    """Create an API with isolated app-level services and injectable tools."""
    resolved_auth_required, resolved_api_key, resolved_docs_enabled, resolved_cors_origins = (
        resolve_auth_config(
            auth_required=auth_required,
            api_key=api_key,
            docs_enabled=docs_enabled,
            cors_origins=cors_origins,
        )
    )
    resolved_interpreter = interpreter if interpreter is not None else interpret_intent
    resolved_order_lookup = order_lookup if order_lookup is not None else query_order

    db_path = os.getenv("AGENT_DB_PATH")
    knowledge_enabled = os.getenv("AGENT_KNOWLEDGE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    if knowledge_search is not None:
        knowledge_enabled = True
    knowledge_dir = os.getenv("AGENT_KNOWLEDGE_DIR", "/app/knowledge_docs")
    knowledge_runtime: dict[str, KnowledgeSearch | None] = {"search": knowledge_search}
    knowledge_error: list[Exception | None] = [None]
    answerer = knowledge_answerer
    using_sqlite = (
        bool(db_path)
        and session_store is None
        and exchange_creator is None
        and refund_creator is None
    )

    runtime = {"session_store": session_store, "exchange_creator": None, "refund_creator": None}

    if runtime["session_store"] is None:
        if using_sqlite:
            runtime["session_store"] = None
        else:
            runtime["session_store"] = InMemorySessionStore()

    if exchange_creator is None:
        if using_sqlite:
            runtime["exchange_creator"] = None
        else:
            exchange_service = InMemoryExchangeService()
            runtime["exchange_creator"] = exchange_service.create_request
    else:
        runtime["exchange_creator"] = exchange_creator

    if refund_creator is None:
        if using_sqlite:
            runtime["refund_creator"] = None
        else:
            refund_service = InMemoryRefundService()
            runtime["refund_creator"] = refund_service.create_request
    else:
        runtime["refund_creator"] = refund_creator

    def _ensure_sqlite_resources() -> None:
        if not using_sqlite:
            return
        assert db_path is not None
        if runtime["session_store"] is None:
            sqlite_session_store = SQLiteSessionStore(db_path)
            sqlite_session_store.initialize()
            runtime["session_store"] = sqlite_session_store

        if runtime["exchange_creator"] is None:
            exchange_service = SQLiteExchangeService(db_path)
            exchange_service.initialize()
            runtime["exchange_creator"] = exchange_service.create_request

        if runtime["refund_creator"] is None:
            refund_service = SQLiteRefundService(db_path)
            refund_service.initialize()
            runtime["refund_creator"] = refund_service.create_request

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        _ensure_sqlite_resources()
        if knowledge_enabled:
            if not db_path:
                knowledge_error[0] = RuntimeError("AGENT_DB_PATH is required when knowledge is enabled")
            elif knowledge_runtime["search"] is None:
                try:
                    service = KnowledgeSearchService(db_path, knowledge_dir)
                    service.initialize_and_sync()
                    knowledge_runtime["search"] = service.search
                except Exception as exc:
                    knowledge_error[0] = exc
        yield

    app = FastAPI(
        title="Customer Service Agent API",
        description=(
            "Uses persistent SQLite state when AGENT_DB_PATH is set "
            "and keeps in-memory fallback otherwise."
        ),
        lifespan=lifespan,
        docs_url="/docs" if resolved_docs_enabled else None,
        redoc_url="/redoc" if resolved_docs_enabled else None,
        openapi_url="/openapi.json" if resolved_docs_enabled else None,
    )
    if resolved_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "X-API-Key"],
        )
    require_api_key = api_key_dependency(resolved_api_key, resolved_auth_required)
    session_locks: dict[str, asyncio.Lock] = {}
    not_ready_message = "Session store is not initialized"

    def get_state(session_id: str) -> ConversationState:
        try:
            _ensure_sqlite_resources()
            return runtime["session_store"].get(session_id)  # type: ignore[union-attr]
        except SessionNotFoundError:
            raise HTTPException(status_code=404, detail="Session not found") from None

    def get_state_with_version(
        session_id: str,
    ) -> tuple[ConversationState, int | None]:
        store = runtime["session_store"]
        if store is None:
            _ensure_sqlite_resources()
            store = runtime["session_store"]
            if store is None:
                raise RuntimeError(not_ready_message)
        if store is None:
            raise RuntimeError(not_ready_message)
        get_with_version = getattr(store, "get_with_version", None)
        if get_with_version is None:
            return store.get(session_id), None  # type: ignore[union-attr]
        try:
            return get_with_version(session_id)
        except SessionNotFoundError:
            raise HTTPException(status_code=404, detail="Session not found") from None

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/knowledge/search", response_model=KnowledgeSearchResponse, dependencies=[Depends(require_api_key)])
    async def search_knowledge(request: KnowledgeSearchRequest) -> KnowledgeSearchResponse:
        search = knowledge_runtime["search"]
        if not knowledge_enabled or search is None or knowledge_error[0] is not None:
            raise HTTPException(status_code=503, detail="Knowledge search is unavailable")
        try:
            hits = search(request.query, max(1, min(5, request.top_k)))
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid knowledge search request") from None
        except Exception:
            raise HTTPException(status_code=503, detail="Knowledge search is temporarily unavailable") from None
        return KnowledgeSearchResponse(hits=hits)

    @app.post("/sessions", response_model=SessionResponse, dependencies=[Depends(require_api_key)])
    async def create_session() -> SessionResponse:
        _ensure_sqlite_resources()
        if runtime["session_store"] is None:
            raise HTTPException(status_code=503, detail="Session store not initialized")
        session_id = runtime["session_store"].create()  # type: ignore[union-attr]
        return _public_snapshot(session_id, runtime["session_store"].get(session_id))  # type: ignore[union-attr]

    @app.get(
        "/sessions/{session_id}",
        response_model=SessionResponse,
        dependencies=[Depends(require_api_key)],
    )
    async def get_session(session_id: str) -> SessionResponse:
        return _public_snapshot(session_id, get_state(session_id))

    @app.post(
        "/sessions/{session_id}/messages",
        response_model=SessionResponse,
        dependencies=[Depends(require_api_key)],
    )
    async def send_message(
        session_id: str,
        request: MessageRequest,
    ) -> SessionResponse:
        _ensure_sqlite_resources()
        lock = session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            state, expected_version = get_state_with_version(session_id)
            resolved_knowledge_search = knowledge_runtime["search"]
            resolved_knowledge_answerer = answerer if answerer is not None else answer_question
            if state.status in TERMINAL_STATUSES:
                run_traced_message(
                    state,
                    request.message,
                    resolved_interpreter,
                    resolved_order_lookup,
                    runtime["exchange_creator"],  # type: ignore[arg-type]
                    runtime["refund_creator"],  # type: ignore[arg-type]
                    trace_sink=trace_sink,
                    knowledge_search=resolved_knowledge_search,
                    knowledge_answerer=resolved_knowledge_answerer,
                )
                return _public_snapshot(session_id, state)

            try:
                next_state, _ = run_traced_message(
                    state,
                    request.message,
                    resolved_interpreter,
                    resolved_order_lookup,
                    runtime["exchange_creator"],  # type: ignore[arg-type]
                    runtime["refund_creator"],  # type: ignore[arg-type]
                    trace_sink=trace_sink,
                    knowledge_search=resolved_knowledge_search,
                    knowledge_answerer=resolved_knowledge_answerer,
                )
            except Exception:
                raise HTTPException(
                    status_code=503,
                    detail="Customer service processing is temporarily unavailable",
                ) from None

            try:
                if expected_version is None:
                    runtime["session_store"].save(session_id, next_state)  # type: ignore[union-attr]
                else:
                    runtime["session_store"].save(
                        session_id, next_state, expected_version=expected_version
                    )  # type: ignore[union-attr]
            except SessionConcurrencyError:
                raise HTTPException(
                    status_code=409,
                    detail="Session state was updated concurrently",
                ) from None

            return _public_snapshot(session_id, next_state)

    return app


app = create_app()
