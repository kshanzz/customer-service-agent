from chat import run_chat
from order_tools import query_order
from schemas import IntentResult


class StubInterpreter:
    def __init__(self):
        self.call_count = 0

    def __call__(self, user_message: str) -> IntentResult:
        self.call_count += 1
        return IntentResult(
            intent="exchange",
            product="耳机",
            reason="左边没有声音",
            missing_information=["order_id"],
        )


def test_two_turn_chat_reaches_ready_without_real_model():
    user_messages = iter(["我的耳机左边没声音，想换货", "A1001"])
    assistant_messages: list[str] = []
    interpreter = StubInterpreter()

    state = run_chat(
        input_func=lambda: next(user_messages),
        output_func=assistant_messages.append,
        interpreter=interpreter,
    )

    assert assistant_messages == [
        "请提供订单号",
        "信息已补全，准备查询订单 A1001",
    ]
    assert state.status == "ready"
    assert state.intent_result is not None
    assert state.intent_result.order_id == "A1001"
    assert interpreter.call_count == 1


def test_exit_ends_chat_without_calling_interpreter():
    interpreter = StubInterpreter()

    state = run_chat(
        input_func=lambda: "/exit",
        output_func=lambda message: None,
        interpreter=interpreter,
    )

    assert state.status == "new"
    assert interpreter.call_count == 0


def test_v2_chat_stops_after_order_is_checked():
    user_messages = iter(["我的耳机左边没声音，想换货", "A1001"])
    assistant_messages: list[str] = []

    state = run_chat(
        input_func=lambda: next(user_messages),
        output_func=assistant_messages.append,
        interpreter=StubInterpreter(),
        order_lookup=query_order,
    )

    assert state.status == "order_checked"
    assert state.order is not None
    assert state.order.order_id == "A1001"
    assert "符合演示换货条件" in assistant_messages[-1]
