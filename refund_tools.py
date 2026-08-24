from typing import Literal

from pydantic import BaseModel, ConfigDict


class RefundRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    order_id: str
    reason: str
    status: Literal["created"] = "created"


class InMemoryRefundService:
    """Session-local, idempotent refund request storage for the demo."""

    def __init__(self) -> None:
        self._requests_by_order: dict[str, RefundRequest] = {}

    @property
    def request_count(self) -> int:
        return len(self._requests_by_order)

    def create_request(self, order_id: str, reason: str) -> RefundRequest:
        normalized_order_id = order_id.upper()
        existing_request = self._requests_by_order.get(normalized_order_id)
        if existing_request is not None:
            return existing_request

        request = RefundRequest(
            request_id=f"RF-{len(self._requests_by_order) + 1:04d}",
            order_id=normalized_order_id,
            reason=reason,
        )
        self._requests_by_order[normalized_order_id] = request
        return request
