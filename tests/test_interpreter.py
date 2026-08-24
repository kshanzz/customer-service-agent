import json

import pytest
from pydantic import ValidationError

from interpreter import create_client, parse_intent_response


def test_create_client_requires_llm_api_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        create_client()


def test_parse_intent_response_accepts_valid_json():
    result = parse_intent_response(
        json.dumps(
            {
                "intent": "exchange",
                "product": "耳机",
                "reason": "左边没有声音",
                "order_id": None,
                "missing_information": ["order_id"],
            }
        )
    )

    assert result.intent == "exchange"
    assert result.product == "耳机"
    assert result.missing_information == ["order_id"]


def test_parse_intent_response_rejects_invalid_intent():
    with pytest.raises(ValidationError):
        parse_intent_response('{"intent": "repair"}')


def test_parse_intent_response_rejects_invalid_field_type():
    with pytest.raises(ValidationError):
        parse_intent_response(
            '{"intent": "exchange", "missing_information": "order_id"}'
        )


def test_parse_intent_response_rejects_invalid_json():
    with pytest.raises(json.JSONDecodeError):
        parse_intent_response('{"intent": "exchange"')
