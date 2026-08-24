import pytest

from exchange_tools import InMemoryExchangeService
from order_tools import query_order
from schemas import ConversationState, IntentResult
from workflow import process_message


class SequenceInterpreter:
    def __init__(self, *results: IntentResult):
        self.results = iter(results)
        self.call_count = 0

    def __call__(self, user_message: str) -> IntentResult:
        self.call_count += 1
        return next(self.results)


class CountingTool:
    def __init__(self, result=None):
        self.calls: list[tuple] = []
        self.result = result

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


def result(intent: str, order_id: str | None = None, missing=None) -> IntentResult:
    return IntentResult(
        intent=intent,
        order_id=order_id,
        reason="测试原因",
        missing_information=[] if missing is None else missing,
    )


def test_complaint_without_order_id_is_ready_and_does_not_ask_for_it():
    interpreter = SequenceInterpreter(result("complaint", missing=["order_id"]))

    state = process_message(ConversationState(), "我要投诉服务态度", interpreter)

    assert state.status == "ready"
    assert state.assistant_message == "投诉信息已识别，准备转交处理"
    assert state.intent_result is not None
    assert state.intent_result.missing_information == []


@pytest.mark.parametrize(
    ("intent", "message"),
    [("refund", "我要退款"), ("logistics", "我的包裹到哪里了")],
)
def test_order_based_intents_without_order_id_ask_for_it(intent, message):
    interpreter = SequenceInterpreter(result(intent, missing=[]))

    state = process_message(ConversationState(), message, interpreter)

    assert state.status == "waiting_for_information"
    assert state.assistant_message == "请提供订单号"
    assert state.intent_result is not None
    assert state.intent_result.missing_information == ["order_id"]


@pytest.mark.parametrize(
    ("intent", "expected_message"),
    [
        ("refund", "信息已补全，准备处理订单 A1001 的退款"),
        ("logistics", "信息已补全，准备查询订单 A1001 的物流"),
    ],
)
def test_second_turn_adds_order_id_without_reinterpreting(intent, expected_message):
    interpreter = SequenceInterpreter(result(intent))
    waiting = process_message(ConversationState(), "第一轮", interpreter)

    ready = process_message(waiting, "订单号 A1001", interpreter)

    assert ready.status == "ready"
    assert ready.assistant_message == expected_message
    assert ready.intent_result is not None
    assert ready.intent_result.order_id == "A1001"
    assert ready.intent_result.intent == intent
    assert interpreter.call_count == 1


def test_unknown_can_be_reinterpreted_on_next_turn():
    interpreter = SequenceInterpreter(
        result("unknown", missing=["order_id"]),
        result("complaint", missing=["order_id"]),
    )

    unknown = process_message(ConversationState(), "帮帮我", interpreter)
    recognized = process_message(unknown, "我要投诉客服", interpreter)

    assert unknown.status == "new"
    assert "请重新说明" in unknown.assistant_message
    assert recognized.status == "ready"
    assert recognized.intent_result is not None
    assert recognized.intent_result.intent == "complaint"
    assert interpreter.call_count == 2


@pytest.mark.parametrize(
    ("model_result", "expected_missing"),
    [
        (result("exchange", missing=[]), ["order_id"]),
        (result("refund", "A1001", ["order_id"]), []),
        (result("complaint", missing=["order_id", "reason"]), []),
        (result("unknown", missing=["order_id"]), []),
    ],
)
def test_model_missing_information_is_recomputed(model_result, expected_missing):
    state = process_message(
        ConversationState(),
        "测试消息",
        SequenceInterpreter(model_result),
    )

    assert state.intent_result is not None
    assert state.intent_result.missing_information == expected_missing


def test_insufficient_information_never_calls_order_or_exchange_tools():
    lookup = CountingTool()
    creator = CountingTool()

    state = process_message(
        ConversationState(),
        "我要换货",
        SequenceInterpreter(result("exchange", missing=[])),
        lookup,
        creator,
    )

    assert state.status == "waiting_for_information"
    assert lookup.calls == []
    assert creator.calls == []


def test_exchange_v3_full_flow_remains_intact():
    interpreter = SequenceInterpreter(result("exchange", "A1001"))
    service = InMemoryExchangeService()

    confirmation = process_message(
        ConversationState(),
        "订单 A1001 要换货",
        interpreter,
        query_order,
        service.create_request,
    )
    completed = process_message(
        confirmation,
        "确认",
        interpreter,
        query_order,
        service.create_request,
    )

    assert confirmation.status == "waiting_for_confirmation"
    assert confirmation.exchange_request is None
    assert completed.status == "completed"
    assert completed.exchange_request is not None
    assert completed.exchange_request.order_id == "A1001"
    assert service.request_count == 1
    assert interpreter.call_count == 1
