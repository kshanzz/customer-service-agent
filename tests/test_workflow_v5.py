from chat import run_chat
from exchange_tools import InMemoryExchangeService
from order_tools import OrderRecord, query_order
from schemas import ConversationState, IntentResult
from workflow import process_message


class SequenceInterpreter:
    def __init__(self, *results: IntentResult):
        self.results = iter(results)
        self.call_count = 0

    def __call__(self, user_message: str) -> IntentResult:
        self.call_count += 1
        return next(self.results)


class CountingOrderLookup:
    def __init__(self, result: OrderRecord | None):
        self.result = result
        self.calls: list[str] = []

    def __call__(self, order_id: str) -> OrderRecord | None:
        self.calls.append(order_id)
        return self.result


class CountingExchangeCreator:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, order_id: str, reason: str):
        self.calls.append((order_id, reason))
        raise AssertionError("物流查询不应创建换货申请")


def logistics_result(order_id: str | None) -> IntentResult:
    return IntentResult(intent="logistics", order_id=order_id)


def test_delivered_logistics_order_is_answered_with_delivery_age():
    order = query_order("A1001")
    assert order is not None
    lookup = CountingOrderLookup(order)

    state = process_message(
        ConversationState(),
        "订单 A1001 的物流",
        SequenceInterpreter(logistics_result("A1001")),
        lookup,
    )

    assert state.status == "answered"
    assert state.order == order
    assert state.assistant_message == "订单 A1001 已经签收（3 天前签收）"
    assert lookup.calls == ["A1001"]


def test_shipped_logistics_order_is_answered():
    order = query_order("C3003")
    assert order is not None
    lookup = CountingOrderLookup(order)

    state = process_message(
        ConversationState(),
        "订单 C3003 的物流",
        SequenceInterpreter(logistics_result("C3003")),
        lookup,
    )

    assert state.status == "answered"
    assert state.order == order
    assert state.assistant_message == "订单 C3003 正在运输中"
    assert lookup.calls == ["C3003"]


def test_cancelled_logistics_order_is_answered():
    order = OrderRecord(
        order_id="D4004",
        product="测试商品",
        status="cancelled",
    )
    lookup = CountingOrderLookup(order)

    state = process_message(
        ConversationState(),
        "订单 D4004 的物流",
        SequenceInterpreter(logistics_result("D4004")),
        lookup,
    )

    assert state.status == "answered"
    assert state.order == order
    assert state.assistant_message == "订单 D4004 已经取消"
    assert lookup.calls == ["D4004"]


def test_unknown_logistics_order_is_answered_without_order_record():
    lookup = CountingOrderLookup(None)

    state = process_message(
        ConversationState(),
        "订单 Z9999 的物流",
        SequenceInterpreter(logistics_result("Z9999")),
        lookup,
    )

    assert state.status == "answered"
    assert state.order is None
    assert state.assistant_message == "未查询到订单 Z9999"
    assert lookup.calls == ["Z9999"]


def test_missing_logistics_order_id_does_not_query_until_second_turn():
    interpreter = SequenceInterpreter(logistics_result(None))
    order = query_order("A1001")
    assert order is not None
    lookup = CountingOrderLookup(order)

    waiting = process_message(
        ConversationState(),
        "帮我查物流",
        interpreter,
        lookup,
    )
    assert waiting.status == "waiting_for_information"
    assert lookup.calls == []

    answered = process_message(waiting, "订单号 A1001", interpreter, lookup)

    assert lookup.calls == ["A1001"]
    assert answered.status == "answered"
    assert answered.order == order
    assert interpreter.call_count == 1


def test_answered_logistics_does_not_repeat_interpreter_or_order_lookup():
    interpreter = SequenceInterpreter(logistics_result("A1001"))
    order = query_order("A1001")
    assert order is not None
    lookup = CountingOrderLookup(order)
    creator = CountingExchangeCreator()

    answered = process_message(
        ConversationState(),
        "订单 A1001 的物流",
        interpreter,
        lookup,
        creator,
    )
    final_state = process_message(
        answered,
        "再查一次",
        interpreter,
        lookup,
        creator,
    )

    assert final_state.status == "answered"
    assert final_state.assistant_message == answered.assistant_message
    assert interpreter.call_count == 1
    assert lookup.calls == ["A1001"]
    assert creator.calls == []


def test_logistics_without_order_lookup_keeps_v4_ready_behavior():
    state = process_message(
        ConversationState(),
        "订单 A1001 的物流",
        SequenceInterpreter(logistics_result("A1001")),
    )

    assert state.status == "ready"
    assert state.assistant_message == "信息已补全，准备查询订单 A1001 的物流"


def test_v3_exchange_confirmation_flow_still_completes():
    interpreter = SequenceInterpreter(
        IntentResult(intent="exchange", order_id="A1001", reason="测试原因")
    )
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
    assert completed.status == "completed"
    assert completed.exchange_request is not None
    assert service.request_count == 1


def test_chat_prints_logistics_answer_then_ends():
    messages: list[str] = []
    state = run_chat(
        input_func=lambda: "订单 A1001 的物流",
        output_func=messages.append,
        interpreter=SequenceInterpreter(logistics_result("A1001")),
        order_lookup=query_order,
    )

    assert state.status == "answered"
    assert messages == ["订单 A1001 已经签收（3 天前签收）"]
