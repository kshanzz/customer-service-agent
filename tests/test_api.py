from collections.abc import Callable
from importlib import reload
from fastapi import FastAPI
import sys

import httpx
import pytest

from api import create_app
from exchange_tools import InMemoryExchangeService
from order_tools import query_order
from refund_tools import InMemoryRefundService
from schemas import ConversationState, IntentResult
from session_store import InMemorySessionStore


pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _clear_agent_db_path(monkeypatch):
    monkeypatch.delenv("AGENT_DB_PATH", raising=False)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


class SequenceInterpreter:
    def __init__(self, *results: IntentResult | Exception):
        self.results = iter(results)
        self.calls: list[str] = []

    def __call__(self, message: str) -> IntentResult:
        self.calls.append(message)
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


class CountingLookup:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, order_id: str):
        self.calls.append(order_id)
        return query_order(order_id)


class CountingCreator:
    def __init__(self, creator: Callable):
        self.creator = creator
        self.calls: list[tuple[str, str]] = []

    def __call__(self, order_id: str, reason: str):
        self.calls.append((order_id, reason))
        return self.creator(order_id, reason)


class FailingTool:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        raise AssertionError("终态或健康检查不应调用工具")


async def test_default_api_app_importable_without_llm_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    api_module = sys.modules.get("api")
    assert api_module is not None
    reloaded = reload(api_module)

    assert isinstance(reloaded.app, FastAPI)
    client = make_client(reloaded.app)

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def create_session(client: httpx.AsyncClient) -> str:
    response = await client.post("/sessions")
    assert response.status_code == 200
    return response.json()["session_id"]


async def test_health_does_not_call_interpreter_or_tools():
    interpreter = FailingTool()
    lookup = FailingTool()
    exchange_creator = FailingTool()
    refund_creator = FailingTool()
    client = make_client(
        create_app(
            interpreter=interpreter,
            order_lookup=lookup,
            exchange_creator=exchange_creator,
            refund_creator=refund_creator,
        )
    )

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert interpreter.calls == []
    assert lookup.calls == []
    assert exchange_creator.calls == []
    assert refund_creator.calls == []


async def test_create_and_get_session_public_snapshot():
    client = make_client(create_app(interpreter=FailingTool()))

    create_response = await client.post("/sessions")
    session_id = create_response.json()["session_id"]
    get_response = await client.get(f"/sessions/{session_id}")

    assert create_response.status_code == 200
    assert get_response.status_code == 200
    assert get_response.json() == create_response.json()
    assert set(get_response.json()) == {
        "session_id",
        "status",
        "assistant_message",
        "intent",
        "order_id",
        "pending_action",
        "request_id",
    }
    assert get_response.json()["status"] == "new"


async def test_unknown_session_returns_404():
    client = make_client(create_app(interpreter=FailingTool()))

    get_response = await client.get("/sessions/missing")
    message_response = await client.post(
        "/sessions/missing/messages",
        json={"message": "你好"},
    )

    assert get_response.status_code == 404
    assert message_response.status_code == 404
    assert get_response.json() == {"detail": "Session not found"}


@pytest.mark.parametrize("message", ["", "   ", "\n\t"])
async def test_blank_message_returns_422_without_interpreting(message):
    interpreter = FailingTool()
    client = make_client(create_app(interpreter=interpreter))
    session_id = await create_session(client)

    response = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": message},
    )

    assert response.status_code == 422
    assert interpreter.calls == []


async def test_two_sessions_keep_independent_state():
    def interpreter(message: str) -> IntentResult:
        if "换货" in message:
            return IntentResult(intent="exchange", reason="测试换货")
        return IntentResult(intent="complaint")

    client = make_client(create_app(interpreter=interpreter))
    first_id = await create_session(client)
    second_id = await create_session(client)

    first_response = await client.post(
        f"/sessions/{first_id}/messages",
        json={"message": "我要换货"},
    )
    second_before = await client.get(f"/sessions/{second_id}")
    second_response = await client.post(
        f"/sessions/{second_id}/messages",
        json={"message": "我要投诉"},
    )
    first_after = await client.get(f"/sessions/{first_id}")

    assert first_response.json()["status"] == "waiting_for_information"
    assert second_before.json()["status"] == "new"
    assert second_response.json()["status"] == "ready"
    assert second_response.json()["intent"] == "complaint"
    assert first_after.json() == first_response.json()


async def test_exchange_three_turn_flow():
    interpreter = SequenceInterpreter(
        IntentResult(intent="exchange", reason="耳机故障")
    )
    lookup = CountingLookup()
    exchange_service = InMemoryExchangeService()
    refund_service = InMemoryRefundService()
    exchange_creator = CountingCreator(exchange_service.create_request)
    refund_creator = CountingCreator(refund_service.create_request)
    client = make_client(
        create_app(
            interpreter=interpreter,
            order_lookup=lookup,
            exchange_creator=exchange_creator,
            refund_creator=refund_creator,
        )
    )
    session_id = await create_session(client)

    first = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": "我要换货"},
    )
    second = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": "订单号 A1001"},
    )
    third = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": "确认"},
    )

    assert first.json()["status"] == "waiting_for_information"
    assert second.json()["status"] == "waiting_for_confirmation"
    assert second.json()["pending_action"] == "exchange"
    assert third.json()["status"] == "completed"
    assert third.json()["pending_action"] is None
    assert third.json()["request_id"] == "EX-0001"
    assert interpreter.calls == ["我要换货"]
    assert lookup.calls == ["A1001"]
    assert len(exchange_creator.calls) == 1
    assert refund_creator.calls == []


async def test_refund_three_turn_flow():
    interpreter = SequenceInterpreter(
        IntentResult(intent="refund", reason="不再需要")
    )
    lookup = CountingLookup()
    exchange_service = InMemoryExchangeService()
    refund_service = InMemoryRefundService()
    exchange_creator = CountingCreator(exchange_service.create_request)
    refund_creator = CountingCreator(refund_service.create_request)
    client = make_client(
        create_app(
            interpreter=interpreter,
            order_lookup=lookup,
            exchange_creator=exchange_creator,
            refund_creator=refund_creator,
        )
    )
    session_id = await create_session(client)

    first = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": "我要退款"},
    )
    second = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": "订单号 A1001"},
    )
    third = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": "确认"},
    )

    assert first.json()["status"] == "waiting_for_information"
    assert second.json()["status"] == "waiting_for_confirmation"
    assert second.json()["pending_action"] == "refund"
    assert third.json()["status"] == "completed"
    assert third.json()["request_id"] == "RF-0001"
    assert interpreter.calls == ["我要退款"]
    assert lookup.calls == ["A1001"]
    assert exchange_creator.calls == []
    assert len(refund_creator.calls) == 1


async def test_logistics_query_flow():
    interpreter = SequenceInterpreter(
        IntentResult(intent="logistics", order_id="C3003")
    )
    lookup = CountingLookup()
    client = make_client(
        create_app(interpreter=interpreter, order_lookup=lookup)
    )
    session_id = await create_session(client)

    response = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": "查询订单 C3003 的物流"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "answered"
    assert response.json()["intent"] == "logistics"
    assert response.json()["order_id"] == "C3003"
    assert response.json()["assistant_message"] == "订单 C3003 正在运输中"
    assert lookup.calls == ["C3003"]


async def test_unknown_intent_is_reinterpreted_on_next_message():
    interpreter = SequenceInterpreter(
        IntentResult(intent="unknown"),
        IntentResult(intent="complaint"),
    )
    client = make_client(create_app(interpreter=interpreter))
    session_id = await create_session(client)

    first = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": "帮帮我"},
    )
    second = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": "我要投诉"},
    )

    assert first.json()["status"] == "new"
    assert first.json()["intent"] == "unknown"
    assert second.json()["status"] == "ready"
    assert second.json()["intent"] == "complaint"
    assert interpreter.calls == ["帮帮我", "我要投诉"]


async def test_interpreter_failure_returns_safe_error_without_losing_state():
    interpreter = SequenceInterpreter(
        IntentResult(intent="unknown"),
        RuntimeError("secret-api-key-value"),
    )
    store = InMemorySessionStore()
    client = make_client(create_app(interpreter=interpreter, session_store=store))
    session_id = await create_session(client)
    first = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": "帮帮我"},
    )

    failed = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": "再试一次"},
    )
    after = await client.get(f"/sessions/{session_id}")

    assert failed.status_code == 503
    assert failed.json() == {
        "detail": "Customer service processing is temporarily unavailable"
    }
    assert "secret-api-key-value" not in failed.text
    assert after.json() == first.json()


async def test_missing_llm_configuration_is_safe_and_keeps_new_state(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = make_client(create_app())
    session_id = await create_session(client)

    failed = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": "我要投诉"},
    )
    after = await client.get(f"/sessions/{session_id}")

    assert failed.status_code == 503
    assert failed.json() == {
        "detail": "Customer service processing is temporarily unavailable"
    }
    assert "LLM_API_KEY" not in failed.text
    assert after.json()["status"] == "new"
    assert after.json()["intent"] is None


@pytest.mark.parametrize(
    "status",
    ["ready", "order_checked", "completed", "cancelled", "rejected", "answered"],
)
async def test_terminal_session_does_not_repeat_any_tool(status):
    store = InMemorySessionStore()
    session_id = store.create()
    store.save(
        session_id,
        ConversationState(status=status, assistant_message="终态结果"),
    )
    interpreter = FailingTool()
    lookup = FailingTool()
    exchange_creator = FailingTool()
    refund_creator = FailingTool()
    client = make_client(
        create_app(
            interpreter=interpreter,
            order_lookup=lookup,
            exchange_creator=exchange_creator,
            refund_creator=refund_creator,
            session_store=store,
        )
    )

    response = await client.post(
        f"/sessions/{session_id}/messages",
        json={"message": "继续处理"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == status
    assert response.json()["assistant_message"] == "终态结果"
    assert interpreter.calls == []
    assert lookup.calls == []
    assert exchange_creator.calls == []
    assert refund_creator.calls == []
