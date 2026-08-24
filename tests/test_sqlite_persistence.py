from importlib import reload
import asyncio
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor
import sys

import httpx
import pytest

from api import create_app
from schemas import ConversationState, IntentResult
from session_store import SessionNotFoundError
from sqlite_store import SQLiteExchangeService, SQLiteRefundService, SQLiteSessionStore


pytestmark = pytest.mark.anyio


def make_client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


def _db_path(tmp_path: Path, name: str) -> str:
    return str(tmp_path / name)


class SequenceInterpreter:
    def __init__(self, *results: IntentResult):
        self.results = iter(results)
        self.calls: list[str] = []

    def __call__(self, message: str) -> IntentResult:
        self.calls.append(message)
        return next(self.results)


class FailingTool:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        raise AssertionError("这个测试场景不应调用该工具")


async def create_session(client: httpx.AsyncClient) -> str:
    response = await client.post("/sessions")
    assert response.status_code == 200
    return response.json()["session_id"]


def test_sqlite_session_persistence_across_store_instances(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path, "customer-service-agent.db")
    first_store = SQLiteSessionStore(db_path)
    first_store.initialize()

    session_id = first_store.create()
    first_store.save(session_id, ConversationState(status="ready", assistant_message="ok"))

    second_store = SQLiteSessionStore(db_path)
    second_store.initialize()
    state = second_store.get(session_id)

    assert state.status == "ready"
    assert state.assistant_message == "ok"


def test_session_status_updates_persist_in_sqlite(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path, "customer-service-agent.db")
    store = SQLiteSessionStore(db_path)
    store.initialize()

    session_id = store.create()
    store.save(session_id, ConversationState(status="waiting_for_information"))
    assert store.get(session_id).status == "waiting_for_information"


def test_sqlite_session_unknown_id_raises(tmp_path: Path) -> None:
    store = SQLiteSessionStore(_db_path(tmp_path, "customer-service-agent.db"))
    store.initialize()

    with pytest.raises(SessionNotFoundError):
        store.get("missing")


def test_exchange_request_idempotent_across_service_instances(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path, "customer-service-agent.db")
    first = SQLiteExchangeService(db_path)
    first.initialize()

    first_request = first.create_request("a1001", "第一次")
    second = SQLiteExchangeService(db_path)
    second.initialize()
    second_request = second.create_request("A1001", "重复")

    assert second_request.request_id == first_request.request_id
    assert second_request.order_id == "A1001"
    assert second_request.reason == "第一次"
    assert first.request_count == 1
    assert second.request_count == 1


def test_refund_request_idempotent_across_service_instances(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path, "customer-service-agent.db")
    first = SQLiteRefundService(db_path)
    first.initialize()

    first_request = first.create_request("a2002", "第一次")
    second = SQLiteRefundService(db_path)
    second.initialize()
    second_request = second.create_request("A2002", "重复")

    assert second_request.request_id == first_request.request_id
    assert second_request.order_id == "A2002"
    assert second_request.reason == "第一次"
    assert first.request_count == 1
    assert second.request_count == 1


def test_same_order_create_only_persists_one_exchange_record(tmp_path: Path) -> None:
    service = SQLiteExchangeService(_db_path(tmp_path, "customer-service-agent.db"))
    service.initialize()
    first = service.create_request("A1001", "原因一")
    second = service.create_request("A1001", "原因二")

    assert service.request_count == 1
    assert second.request_id == first.request_id


def test_same_order_create_only_persists_one_refund_record(tmp_path: Path) -> None:
    service = SQLiteRefundService(_db_path(tmp_path, "customer-service-agent.db"))
    service.initialize()
    first = service.create_request("A1001", "原因一")
    second = service.create_request("A1001", "原因二")

    assert service.request_count == 1
    assert second.request_id == first.request_id


def _run_concurrent_service_creates(
    first_service,
    second_service,
    order_id: str,
    reason: str,
):
    start_barrier = threading.Barrier(2)

    def _worker(service):
        start_barrier.wait()
        return service.create_request(order_id, reason)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_worker, first_service),
            executor.submit(_worker, second_service),
        ]
        return futures[0].result(), futures[1].result()


def test_exchange_request_concurrent_creates_same_order_tmp_db(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path, "customer-service-agent.db")
    first_service = SQLiteExchangeService(db_path)
    second_service = SQLiteExchangeService(db_path)
    first_service.initialize()
    second_service.initialize()

    first_request, second_request = _run_concurrent_service_creates(
        first_service, second_service, "a1001", "并发换货"
    )

    assert first_request.request_id == second_request.request_id
    assert second_request.order_id == "A1001"
    assert first_request.order_id == "A1001"
    assert first_service.request_count == 1


def test_refund_request_concurrent_creates_same_order_tmp_db(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path, "customer-service-agent.db")
    first_service = SQLiteRefundService(db_path)
    second_service = SQLiteRefundService(db_path)
    first_service.initialize()
    second_service.initialize()

    first_request, second_request = _run_concurrent_service_creates(
        first_service, second_service, "a2002", "并发退款"
    )

    assert first_request.request_id == second_request.request_id
    assert second_request.order_id == "A2002"
    assert first_request.order_id == "A2002"
    assert first_service.request_count == 1


async def test_concurrent_confirmation_calls_in_same_app_only_complete_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = _db_path(tmp_path, "customer-service-agent.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)

    app = create_app(
        interpreter=SequenceInterpreter(
            IntentResult(intent="exchange", reason="耳机故障"),
            IntentResult(intent="exchange", reason="耳机故障"),
        )
    )
    async with make_client(app) as client:
        session_id = await create_session(client)
        first = await client.post(
            f"/sessions/{session_id}/messages",
            json={"message": "我要换货"},
        )
        second = await client.post(
            f"/sessions/{session_id}/messages",
            json={"message": "订单号 A1001"},
        )
        assert first.status_code == 200
        assert second.status_code == 200

        first_confirmation, second_confirmation = await asyncio.gather(
            client.post(
                f"/sessions/{session_id}/messages",
                json={"message": "确认"},
            ),
            client.post(
                f"/sessions/{session_id}/messages",
                json={"message": "确认"},
            ),
        )

    assert first_confirmation.status_code == 200
    assert second_confirmation.status_code == 200
    assert first_confirmation.json()["status"] == "completed"
    assert second_confirmation.json()["status"] == "completed"

    service = SQLiteExchangeService(db_path)
    service.initialize()
    assert service.request_count == 1


async def test_concurrent_confirmation_between_two_apps_with_shared_db(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = _db_path(tmp_path, "customer-service-agent.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)

    first_app = create_app(
        interpreter=SequenceInterpreter(
            IntentResult(intent="exchange", reason="耳机故障"),
            IntentResult(intent="exchange", reason="耳机故障"),
        )
    )

    async with make_client(first_app) as first_client:
        session_id = await create_session(first_client)
        create_response = await first_client.post(
            f"/sessions/{session_id}/messages",
            json={"message": "我要换货"},
        )
        confirm_response = await first_client.post(
            f"/sessions/{session_id}/messages",
            json={"message": "订单号 A1001"},
        )
        assert create_response.status_code == 200
        assert confirm_response.status_code == 200

    second_app = create_app(
        interpreter=SequenceInterpreter(
            IntentResult(intent="exchange", reason="耳机故障"),
            IntentResult(intent="exchange", reason="耳机故障"),
        )
    )

    async with make_client(first_app) as first_client_again, make_client(second_app) as second_client:
        first_confirmation, second_confirmation = await asyncio.gather(
            first_client_again.post(
                f"/sessions/{session_id}/messages",
                json={"message": "确认"},
            ),
            second_client.post(
                f"/sessions/{session_id}/messages",
                json={"message": "确认"},
            ),
        )

    assert first_confirmation.status_code in {200, 409}
    assert second_confirmation.status_code in {200, 409}
    assert not (
        first_confirmation.status_code == 409
        and second_confirmation.status_code == 409
    )
    assert first_confirmation.status_code != 500
    assert second_confirmation.status_code != 500

    session_store = SQLiteSessionStore(db_path)
    session_store.initialize()
    restored = session_store.get(session_id)
    assert restored.status == "completed"

    exchange_service = SQLiteExchangeService(db_path)
    exchange_service.initialize()
    assert exchange_service.request_count == 1


async def test_two_app_instances_share_sqlite_state(tmp_path: Path, monkeypatch):
    db_path = _db_path(tmp_path, "customer-service-agent.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)

    first_app = create_app(
        interpreter=SequenceInterpreter(IntentResult(intent="complaint", reason="反馈")),
    )
    async with make_client(first_app) as first_client:
        session_id = await create_session(first_client)
        create_response = await first_client.post(
            f"/sessions/{session_id}/messages",
            json={"message": "我要投诉"},
        )
        assert create_response.status_code == 200
        assert create_response.json()["status"] == "ready"

    second_app = create_app(interpreter=FailingTool())
    async with make_client(second_app) as second_client:
        restored = await second_client.get(f"/sessions/{session_id}")
        assert restored.status_code == 200
        assert restored.json()["status"] == "ready"
        assert restored.json()["intent"] == "complaint"


def test_api_import_does_not_create_database_file(tmp_path, monkeypatch):
    db = _db_path(tmp_path, "import.db")
    monkeypatch.setenv("AGENT_DB_PATH", db)
    reloaded_api = reload(sys.modules["api"])

    assert reloaded_api is not None
    assert not Path(db).exists()


async def test_default_without_db_path_keeps_memory_mode(monkeypatch):
    monkeypatch.delenv("AGENT_DB_PATH", raising=False)
    first = create_app(
        interpreter=SequenceInterpreter(IntentResult(intent="complaint", reason="反馈")),
    )
    second = create_app(interpreter=FailingTool())

    async with make_client(first) as first_client:
        session_id = await create_session(first_client)
        response = await first_client.post(
            f"/sessions/{session_id}/messages",
            json={"message": "我要投诉"},
        )
        assert response.status_code == 200

    async with make_client(second) as second_client:
        restored = await second_client.get(f"/sessions/{session_id}")
        assert restored.status_code == 404
