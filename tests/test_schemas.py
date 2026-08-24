import pytest
from pydantic import ValidationError

from schemas import IntentResult


def test_valid_intent_result():
    result = IntentResult(
        intent="exchange",
        product="耳机",
        reason="左边没有声音",
        missing_information=["order_id"],
    )

    assert result.intent == "exchange"
    assert result.missing_information == ["order_id"]


def test_rejects_unknown_intent():
    with pytest.raises(ValidationError):
        IntentResult(intent="repair")


def test_rejects_invalid_missing_information_type():
    with pytest.raises(ValidationError):
        IntentResult(
            intent="exchange",
            missing_information="order_id",
        )