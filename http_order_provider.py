"""HTTP-backed, read-only order lookup provider.

The provider deliberately exposes only a callable order lookup operation.  It
does not know about the conversation state or any of the write-side services.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from urllib.parse import urlsplit

import httpx

from order_tools import OrderRecord


ORDER_ID_PATTERN = re.compile(r"[A-Za-z]\d{4}\Z")
ALLOWED_STATUSES = {"delivered", "shipped", "cancelled"}
MAX_RETRY_BACKOFF_SECONDS = 2.0
MAX_RETRY_AFTER_SECONDS = 2.0


class OrderProviderError(RuntimeError):
    """Base class for safe, client-facing order provider failures."""

    http_status = 503
    trace_outcome = "upstream_error"


class OrderProviderTemporaryError(OrderProviderError):
    """A temporary network or upstream availability failure."""

    http_status = 503
    trace_outcome = "upstream_error"


class OrderProviderTimeoutError(OrderProviderTemporaryError):
    """A connect/read timeout or an upstream 408 response."""

    trace_outcome = "timeout"


class OrderProviderUpstreamError(OrderProviderError):
    """A non-retryable upstream response or protocol failure."""

    http_status = 502
    trace_outcome = "upstream_error"


class OrderProviderCircuitOpenError(OrderProviderTemporaryError):
    """The provider circuit is open and the request was not sent."""

    trace_outcome = "circuit_open"


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True)
class OrderProviderSettings:
    provider: str = "memory"
    base_url: str | None = None
    token: str | None = None
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 10.0
    max_attempts: int = 1
    circuit_failure_threshold: int = 5
    circuit_cooldown_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "OrderProviderSettings":
        provider = os.getenv("AGENT_ORDER_PROVIDER", "memory").strip().lower()
        if provider not in {"memory", "http"}:
            raise ValueError("AGENT_ORDER_PROVIDER must be memory or http")

        settings = cls(
            provider=provider,
            base_url=os.getenv("AGENT_ORDER_API_BASE_URL"),
            token=os.getenv("AGENT_ORDER_API_TOKEN"),
            connect_timeout_seconds=_positive_float(
                "AGENT_ORDER_CONNECT_TIMEOUT_SECONDS", 5.0
            ),
            read_timeout_seconds=_positive_float(
                "AGENT_ORDER_READ_TIMEOUT_SECONDS", 10.0
            ),
            max_attempts=_bounded_int("AGENT_ORDER_MAX_ATTEMPTS", 1, 1, 3),
            circuit_failure_threshold=_positive_int(
                "AGENT_ORDER_CIRCUIT_FAILURE_THRESHOLD", 5
            ),
            circuit_cooldown_seconds=_nonnegative_float(
                "AGENT_ORDER_CIRCUIT_COOLDOWN_SECONDS", 30.0
            ),
        )
        if provider == "http":
            if not settings.base_url or not settings.base_url.strip():
                raise ValueError(
                    "AGENT_ORDER_API_BASE_URL is required when order provider is http"
                )
            if not settings.token or not settings.token.strip():
                raise ValueError(
                    "AGENT_ORDER_API_TOKEN is required when order provider is http"
                )
            validate_base_url(settings.base_url)
        return settings


def _positive_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _nonnegative_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be at least one")
    return value


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def validate_base_url(base_url: str) -> str:
    """Validate and normalize an upstream base URL without contacting it."""
    candidate = base_url.strip()
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("AGENT_ORDER_API_BASE_URL is invalid") from exc

    if (
        not parsed.scheme
        or parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in candidate)
    ):
        raise ValueError("AGENT_ORDER_API_BASE_URL is invalid")

    scheme = parsed.scheme.lower()
    hostname = hostname.lower()
    if scheme == "http" and hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("AGENT_ORDER_API_BASE_URL must use HTTPS")
    return candidate.rstrip("/")


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None
    if not isfinite(seconds) or seconds < 0 or seconds > MAX_RETRY_AFTER_SECONDS:
        return None
    return seconds


class HttpOrderProvider:
    """A synchronous, reusable HTTP order lookup callable.

    The circuit breaker is intentionally local to this provider instance.  It
    is therefore valid only inside one process/app instance, not across workers
    or replicas.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 10.0,
        max_attempts: int = 1,
        circuit_failure_threshold: int = 5,
        circuit_cooldown_seconds: float = 30.0,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("order API token is required")
        self.base_url = validate_base_url(base_url)
        self._token = token
        self.connect_timeout_seconds = _validate_positive(
            connect_timeout_seconds, "connect timeout"
        )
        self.read_timeout_seconds = _validate_positive(
            read_timeout_seconds, "read timeout"
        )
        if not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")
        if circuit_failure_threshold < 1:
            raise ValueError("circuit_failure_threshold must be at least one")
        if not isfinite(circuit_cooldown_seconds) or circuit_cooldown_seconds < 0:
            raise ValueError("circuit_cooldown_seconds must not be negative")
        if client is not None and transport is not None:
            raise ValueError("client and transport are mutually exclusive")

        self.max_attempts = max_attempts
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_cooldown_seconds = circuit_cooldown_seconds
        self._sleeper = sleeper
        self._clock = clock
        self._lock = threading.Lock()
        self._circuit_state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_probe_in_flight = False
        self._closed = False

        timeout = httpx.Timeout(
            connect=self.connect_timeout_seconds,
            read=self.read_timeout_seconds,
            write=self.read_timeout_seconds,
            pool=self.connect_timeout_seconds,
        )
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                transport=transport,
                timeout=timeout,
                follow_redirects=False,
            )
            self._owns_client = True

    @classmethod
    def from_env(
        cls,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> "HttpOrderProvider":
        settings = OrderProviderSettings.from_env()
        if settings.provider != "http":
            raise ValueError("HTTP order provider is not enabled")
        assert settings.base_url is not None
        assert settings.token is not None
        return cls(
            settings.base_url,
            settings.token,
            connect_timeout_seconds=settings.connect_timeout_seconds,
            read_timeout_seconds=settings.read_timeout_seconds,
            max_attempts=settings.max_attempts,
            circuit_failure_threshold=settings.circuit_failure_threshold,
            circuit_cooldown_seconds=settings.circuit_cooldown_seconds,
            client=client,
            transport=transport,
            sleeper=sleeper,
            clock=clock,
        )

    @property
    def circuit_state(self) -> CircuitState:
        with self._lock:
            return self._circuit_state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    def _acquire_circuit(self) -> None:
        now = self._clock()
        with self._lock:
            if self._circuit_state == CircuitState.OPEN:
                assert self._opened_at is not None
                if now - self._opened_at < self.circuit_cooldown_seconds:
                    raise OrderProviderCircuitOpenError(
                        "order service circuit is open"
                    )
                self._circuit_state = CircuitState.HALF_OPEN
                self._half_open_probe_in_flight = True
            elif self._circuit_state == CircuitState.HALF_OPEN:
                raise OrderProviderCircuitOpenError(
                    "order service circuit is open"
                )

    def _record_success(self) -> None:
        with self._lock:
            self._circuit_state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_probe_in_flight = False

    def _record_temporary_failure(self) -> None:
        now = self._clock()
        with self._lock:
            if self._circuit_state == CircuitState.HALF_OPEN:
                self._circuit_state = CircuitState.OPEN
                self._opened_at = now
                self._consecutive_failures = self.circuit_failure_threshold
                self._half_open_probe_in_flight = False
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.circuit_failure_threshold:
                self._circuit_state = CircuitState.OPEN
                self._opened_at = now

    def _url_for(self, order_id: str) -> str:
        if not isinstance(order_id, str) or ORDER_ID_PATTERN.fullmatch(order_id) is None:
            raise ValueError("invalid order id")
        return f"{self.base_url}/orders/{order_id.upper()}"

    def _sleep_before_retry(
        self, attempt: int, response: httpx.Response | None
    ) -> None:
        retry_after = _parse_retry_after(
            response.headers.get("Retry-After") if response is not None else None
        )
        delay = retry_after
        if delay is None:
            delay = min(MAX_RETRY_BACKOFF_SECONDS, 0.1 * (2 ** (attempt - 1)))
        self._sleeper(delay)

    def _map_response(self, response: httpx.Response, order_id: str) -> OrderRecord | None:
        status_code = response.status_code
        if status_code == 404:
            return None
        if status_code == 200:
            try:
                payload = response.json()
            except (ValueError, TypeError):
                raise OrderProviderUpstreamError(
                    "order service returned an invalid response"
                ) from None
            if not isinstance(payload, dict):
                raise OrderProviderUpstreamError(
                    "order service returned an invalid response"
                )
            expected = {"order_id", "product", "status", "days_since_delivery"}
            if not expected.issubset(payload):
                raise OrderProviderUpstreamError(
                    "order service returned an invalid response"
                )
            response_order_id = payload["order_id"]
            product = payload["product"]
            status = payload["status"]
            days = payload["days_since_delivery"]
            if (
                not isinstance(response_order_id, str)
                or response_order_id.upper() != order_id.upper()
                or ORDER_ID_PATTERN.fullmatch(response_order_id) is None
                or not isinstance(product, str)
                or not isinstance(status, str)
                or status not in ALLOWED_STATUSES
                or (days is not None and (type(days) is not int))
            ):
                raise OrderProviderUpstreamError(
                    "order service returned an invalid response"
                )
            try:
                return OrderRecord(
                    order_id=response_order_id.upper(),
                    product=product,
                    status=status,
                    days_since_delivery=days,
                )
            except (TypeError, ValueError):
                raise OrderProviderUpstreamError(
                    "order service returned an invalid response"
                ) from None
        if status_code == 400 or status_code in {401, 403} or status_code < 500:
            raise OrderProviderUpstreamError("order service returned an error")
        if 500 <= status_code <= 599:
            raise OrderProviderTemporaryError("order service is temporarily unavailable")
        raise OrderProviderUpstreamError("order service returned an invalid response")

    def __call__(self, order_id: str) -> OrderRecord | None:
        url = self._url_for(order_id)
        self._acquire_circuit()
        try:
            for attempt in range(1, self.max_attempts + 1):
                response: httpx.Response | None = None
                try:
                    response = self._client.get(
                        url,
                        headers={
                            "Authorization": f"Bearer {self._token}",
                            "Accept": "application/json",
                        },
                        timeout=httpx.Timeout(
                            connect=self.connect_timeout_seconds,
                            read=self.read_timeout_seconds,
                            write=self.read_timeout_seconds,
                            pool=self.connect_timeout_seconds,
                        ),
                    )
                    if response.status_code in {408, 429} or 500 <= response.status_code <= 599:
                        if attempt < self.max_attempts:
                            self._sleep_before_retry(attempt, response)
                            continue
                        self._record_temporary_failure()
                        error_type = (
                            OrderProviderTimeoutError
                            if response.status_code == 408
                            else OrderProviderTemporaryError
                        )
                        raise error_type(
                            "order service is temporarily unavailable"
                        )
                    result = self._map_response(response, order_id)
                    self._record_success()
                    return result
                except httpx.TimeoutException:
                    if attempt < self.max_attempts:
                        self._sleep_before_retry(attempt, response)
                        continue
                    self._record_temporary_failure()
                    raise OrderProviderTimeoutError(
                        "order service request timed out"
                    ) from None
                except httpx.TransportError:
                    if attempt < self.max_attempts:
                        self._sleep_before_retry(attempt, response)
                        continue
                    self._record_temporary_failure()
                    raise OrderProviderTemporaryError(
                        "order service is temporarily unavailable"
                    ) from None
                except OrderProviderTemporaryError:
                    raise
                except OrderProviderError:
                    self._record_success()
                    raise
                except Exception:
                    # Never propagate a transport/client exception that may
                    # contain request metadata.  The public error is fixed
                    # text and carries no URL, header, or upstream body.
                    self._record_success()
                    raise OrderProviderUpstreamError(
                        "order service returned an invalid response"
                    ) from None
        finally:
            # A failed half-open probe must never leave the breaker stuck in
            # HALF_OPEN if an unexpected client exception escapes.
            with self._lock:
                half_open_stuck = (
                    self._circuit_state == CircuitState.HALF_OPEN
                    and self._half_open_probe_in_flight
                )
            if half_open_stuck:
                self._record_temporary_failure()
        raise OrderProviderTemporaryError("order service is temporarily unavailable")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "HttpOrderProvider":
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()


def _validate_positive(value: float, label: str) -> float:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be greater than zero")
    return float(value)
