import logging

import httpx
import pytest

from api import create_app
from schemas import IntentResult


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


class CountingInterpreter:
    def __init__(self):
        self.calls = 0

    def __call__(self, _message: str) -> IntentResult:
        self.calls += 1
        return IntentResult(intent="complaint")


async def test_auth_disabled_keeps_legacy_behavior():
    async with client_for(create_app(auth_required=False)) as client:
        response = await client.post("/sessions")
    assert response.status_code == 200


async def test_auth_protects_sessions_but_not_health_and_does_not_run_business_code():
    interpreter = CountingInterpreter()
    traces = []
    async with client_for(
        create_app(
            auth_required=True,
            api_key="a" * 32,
            interpreter=interpreter,
            trace_sink=traces.append,
        )
    ) as client:
        assert (await client.get("/health")).status_code == 200
        missing = await client.post("/sessions")
        wrong = await client.post("/sessions", headers={"X-API-Key": "b" * 32})
        unknown = await client.get("/sessions/does-not-exist")
        message = await client.post(
            "/sessions/does-not-exist/messages",
            json={"message": "test"},
        )

    for response in (missing, wrong, unknown, message):
        assert response.status_code == 401
        assert response.json() == {"detail": "Unauthorized"}
        assert "a" * 32 not in response.text
    assert interpreter.calls == 0
    assert traces == []


async def test_correct_key_can_create_read_and_send_message():
    key = "a" * 32
    async with client_for(
        create_app(
            auth_required=True,
            api_key=key,
            interpreter=CountingInterpreter(),
        )
    ) as client:
        headers = {"X-API-Key": key}
        created = await client.post("/sessions", headers=headers)
        session_id = created.json()["session_id"]
        read = await client.get(f"/sessions/{session_id}", headers=headers)
        message = await client.post(
            f"/sessions/{session_id}/messages",
            headers=headers,
            json={"message": "投诉"},
        )

    assert created.status_code == read.status_code == message.status_code == 200
    assert read.json()["session_id"] == session_id
    assert message.json()["status"] == "ready"


def test_required_auth_rejects_missing_short_and_placeholder_keys():
    with pytest.raises(ValueError):
        create_app(auth_required=True)
    with pytest.raises(ValueError):
        create_app(auth_required=True, api_key="a" * 31)
    with pytest.raises(ValueError):
        create_app(auth_required=True, api_key="replace-me")


def test_docs_switch_and_explicit_parameters_take_precedence(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_REQUIRED", "true")
    monkeypatch.setenv("AGENT_API_KEY", "e" * 32)
    monkeypatch.setenv("AGENT_DOCS_ENABLED", "false")
    disabled = create_app(auth_required=False, docs_enabled=True)
    assert disabled.docs_url == "/docs"
    assert disabled.openapi_url == "/openapi.json"

    closed = create_app(auth_required=True, api_key="e" * 32)
    assert closed.docs_url is None
    assert closed.redoc_url is None
    assert closed.openapi_url is None


async def test_cors_is_exact_and_wildcard_is_rejected_when_auth_enabled():
    app = create_app(
        auth_required=True,
        api_key="c" * 32,
        cors_origins="https://allowed.example",
    )
    async with client_for(app) as client:
        allowed = await client.options(
            "/health",
            headers={
                "Origin": "https://allowed.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        denied = await client.options(
            "/health",
            headers={
                "Origin": "https://other.example",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert allowed.headers.get("access-control-allow-origin") == "https://allowed.example"
    assert "access-control-allow-origin" not in denied.headers
    with pytest.raises(ValueError):
        create_app(auth_required=True, api_key="c" * 32, cors_origins="*")


async def test_auth_failure_does_not_log_the_key(caplog):
    key = "secret-key-012345678901234567890123"
    async with client_for(create_app(auth_required=True, api_key=key)) as client:
        with caplog.at_level(logging.INFO):
            response = await client.post("/sessions", headers={"X-API-Key": key + "x"})
    assert response.status_code == 401
    assert key not in response.text
    assert key not in caplog.text
