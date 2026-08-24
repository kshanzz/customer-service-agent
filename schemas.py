from typing import Literal

from pydantic import BaseModel, Field

from exchange_tools import ExchangeRequest
from order_tools import OrderRecord


class IntentResult(BaseModel):
    intent: Literal[
        "exchange",
        "refund",
        "logistics",
        "complaint",
        "unknown",
    ]

    product: str | None = None
    reason: str | None = None
    order_id: str | None = None

    missing_information: list[str] = Field(default_factory=list)


class ConversationState(BaseModel):
    intent_result: IntentResult | None = None
    status: Literal[
        "new",
        "waiting_for_information",
        "ready",
        "order_checked",
        "waiting_for_confirmation",
        "completed",
        "cancelled",
        "rejected",
    ] = "new"
    assistant_message: str | None = None
    order: OrderRecord | None = None
    eligibility_reason: str | None = None
    exchange_request: ExchangeRequest | None = None
