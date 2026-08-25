import json
from collections.abc import Callable

import httpx
import pytest

from api import create_app
from http_order_provider import (
    CircuitState,
    HttpOrderProvider,
    OrderProviderCircuitOpenError,
    OrderProviderSettings,
    OrderProviderTimeoutError,
    OrderProviderUpstreamError,
    validate_base_url,
)
from schemas import IntentResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


def response_handler(
    responses: list[httpx.Response | Exception],
    requests: list[httpx.Request],
) -> Callable[[httpx.Request], httpx.Response]:
    queue = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        result = next(queue)
        if isinstance(result, Exception):
            raise result
        result.request = request
        return result

    return handler


def make_provider(
    responses: list[httpx.Response | Exception],
    *,
    attempts: int = 1,
    threshold: int = 5,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
):
    requests: list[httpx.Request] = []
    transport = httpx.MockTransport(response_handler(responses, requests))
    provider = HttpOrderProvider(
        "https://orders.example.test/api/",
        "token-that-must-not-leak",
        max_attempts=attempts,
        circuit_failure_threshold=threshold,
        circuit_cooldown_seconds=10,
        transport=transport,
        clock=clock or (lambda: 0.0),
        sleeper=sleeper or (lambda _seconds: None),
    )
    return provider, requests


def valid_payload(order_id: str = "A1001") -> dict:
    return {
        "order_id": order_id,
        "product": "耳机",
        "status": "delivered",
        "days_since_delivery": 3,
    }


def test_200_maps_to_order_record_and_sends_fixed_request_headers():
    provider, requests = make_provider(
        [httpx.Response(200, json=valid_payload())]
    )

    order = provider("A1001")

    assert order is not None
    assert order.order_id == "A1001"
    assert order.product == "耳机"
    assert order.status == "delivered"
    assert order.days_since_delivery == 3
    assert str(requests[0].url) == "https://orders.example.test/api/orders/A1001"
    assert requests[0].headers["Authorization"] == "Bearer token-that-must-not-leak"
    assert requests[0].headers["Accept"] == "application/json"


def test_404_returns_none_without_retry():
    sleeps: list[float] = []
    provider, requests = make_provider(
        [httpx.Response(404), httpx.Response(200, json=valid_payload())],
        attempts=3,
        sleeper=sleeps.append,
    )

    assert provider("A1001") is None
    assert len(requests) == 1
    assert sleeps == []


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_non_retryable_http_errors_do_not_retry(status_code: int):
    provider, requests = make_provider(
        [httpx.Response(status_code), httpx.Response(200, json=valid_payload())],
        attempts=3,
    )

    with pytest.raises(OrderProviderUpstreamError):
        provider("A1001")
    assert len(requests) == 1


@pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504])
def test_temporary_statuses_retry_with_bounded_attempts(status_code: int):
    sleeps: list[float] = []
    provider, requests = make_provider(
        [httpx.Response(status_code)] * 3,
        attempts=3,
        sleeper=sleeps.append,
    )

    with pytest.raises(OrderProviderTimeoutError if status_code == 408 else Exception):
        provider("A1001")
    assert len(requests) == 3
    assert len(sleeps) == 2
    assert sleeps == [0.1, 0.2]


def test_retry_success_returns_record():
    provider, requests = make_provider(
        [httpx.Response(503), httpx.Response(200, json=valid_payload())],
        attempts=3,
    )

    assert provider("A1001") is not None
    assert len(requests) == 2
    assert provider.circuit_state == CircuitState.CLOSED


@pytest.mark.parametrize(
    "exception_factory,expected_type",
    [
        (lambda request: httpx.ConnectError("connection failed", request=request), Exception),
        (lambda request: httpx.ReadTimeout("read timed out", request=request), OrderProviderTimeoutError),
    ],
)
def test_connection_failure_and_timeout_retry_then_fail(exception_factory, expected_type):
    requests: list[httpx.Request] = []
    transport = httpx.MockTransport(
        lambda current: (
            requests.append(current),
            (_ for _ in ()).throw(exception_factory(current)),
        )[1]
    )
    provider = HttpOrderProvider(
        "https://orders.example.test",
        "secret-token",
        max_attempts=2,
        transport=transport,
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(expected_type):
        provider("A1001")
    assert len(requests) == 2


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        {"order_id": "A1001", "product": "耳机", "status": "delivered"},
        {**valid_payload(), "status": "unknown"},
        {**valid_payload(), "days_since_delivery": "3"},
    ],
)
def test_invalid_json_or_fields_are_protocol_errors_without_retry(payload):
    if isinstance(payload, str):
        response = httpx.Response(200, content=payload.encode())
    else:
        response = httpx.Response(200, content=json.dumps(payload).encode())
    provider, requests = make_provider([response], attempts=3)

    with pytest.raises(OrderProviderUpstreamError):
        provider("A1001")
    assert len(requests) == 1


def test_order_id_injection_is_rejected_before_http_request():
    provider, requests = make_provider([httpx.Response(200, json=valid_payload())])

    with pytest.raises(ValueError):
        provider("A1001/../../admin")
    assert requests == []


@pytest.mark.parametrize(
    "url",
    [
        "http://orders.example.test",
        "https://user:password@orders.example.test",
        "https://orders.example.test?token=secret",
        "https://orders.example.test#fragment",
    ],
)
def test_base_url_security_validation(url: str):
    with pytest.raises(ValueError):
        validate_base_url(url)


def test_localhost_http_is_allowed_and_production_http_is_not():
    assert validate_base_url("http://localhost:8080/") == "http://localhost:8080"
    assert validate_base_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    assert validate_base_url("https://orders.example.test/") == "https://orders.example.test"


def test_max_attempts_config_is_strictly_bounded(monkeypatch):
    monkeypatch.setenv("AGENT_ORDER_PROVIDER", "http")
    monkeypatch.setenv("AGENT_ORDER_API_BASE_URL", "https://orders.example.test")
    monkeypatch.setenv("AGENT_ORDER_API_TOKEN", "placeholder-token")
    for value in ("0", "4"):
        monkeypatch.setenv("AGENT_ORDER_MAX_ATTEMPTS", value)
        with pytest.raises(ValueError):
            OrderProviderSettings.from_env()


def test_http_mode_requires_base_url_and_token_at_app_creation(monkeypatch):
    monkeypatch.setenv("AGENT_ORDER_PROVIDER", "http")
    monkeypatch.delenv("AGENT_ORDER_API_BASE_URL", raising=False)
    monkeypatch.delenv("AGENT_ORDER_API_TOKEN", raising=False)
    with pytest.raises(ValueError):
        create_app()

    monkeypatch.setenv("AGENT_ORDER_API_BASE_URL", "https://orders.example.test")
    with pytest.raises(ValueError):
        create_app()


def test_circuit_transitions_and_open_skips_http():
    now = [0.0]
    provider, requests = make_provider(
        [httpx.Response(503), httpx.Response(200, json=valid_payload())],
        threshold=1,
        clock=lambda: now[0],
    )

    with pytest.raises(Exception):
        provider("A1001")
    assert provider.circuit_state == CircuitState.OPEN
    with pytest.raises(OrderProviderCircuitOpenError):
        provider("A1001")
    assert len(requests) == 1

    now[0] = 11.0
    assert provider("A1001") is not None
    assert provider.circuit_state == CircuitState.CLOSED
    assert len(requests) == 2


def test_circuit_state_is_isolated_per_provider_instance():
    first, first_requests = make_provider([httpx.Response(503)], threshold=1)
    second, second_requests = make_provider([httpx.Response(200, json=valid_payload())], threshold=1)

    with pytest.raises(Exception):
        first("A1001")
    assert first.circuit_state == CircuitState.OPEN
    assert second("A1001") is not None
    assert len(first_requests) == 1
    assert len(second_requests) == 1


@pytest.mark.anyio
async def test_api_maps_upstream_failure_safely_keeps_state_and_health_does_not_call_upstream():
    requests: list[httpx.Request] = []
    provider, requests = make_provider(
        [httpx.Response(503)], attempts=1, threshold=10
    )
    traces = []
    app = create_app(
        interpreter=lambda _message: IntentResult(
            intent="logistics", order_id="A1001"
        ),
        order_provider=provider,
        trace_sink=traces.append,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        created = await client.post("/sessions")
        session_id = created.json()["session_id"]
        failed = await client.post(
            f"/sessions/{session_id}/messages", json={"message": "查物流"}
        )
        state = await client.get(f"/sessions/{session_id}")

    assert failed.status_code == 503
    assert failed.json() == {"detail": "Order service is temporarily unavailable"}
    assert state.json()["status"] == "new"
    assert len(requests) == 1
    assert traces[-1].events[-2].component == "order_lookup"
    assert traces[-1].events[-2].outcome == "upstream_error"
    trace_text = json.dumps(traces[-1].model_dump(), ensure_ascii=False)
    assert "token-that-must-not-leak" not in trace_text
    assert "Authorization" not in trace_text
    assert "orders.example.test" not in trace_text


@pytest.mark.anyio
async def test_default_memory_provider_remains_unchanged(monkeypatch):
    monkeypatch.delenv("AGENT_ORDER_PROVIDER", raising=False)
    app = create_app(
        interpreter=lambda _message: IntentResult(
            intent="logistics", order_id="A1001"
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post("/sessions")
        session_id = created.json()["session_id"]
        response = await client.post(
            f"/sessions/{session_id}/messages", json={"message": "我要查询物流 A1001"}
        )
    assert response.status_code == 200
