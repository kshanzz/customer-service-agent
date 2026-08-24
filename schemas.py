from typing import Literal

from pydantic import BaseModel, Field


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
    status: Literal["new", "waiting_for_information", "ready"] = "new"
    assistant_message: str | None = None
