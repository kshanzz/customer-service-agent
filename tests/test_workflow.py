from schemas import ConversationState, IntentResult
from workflow import process_message


class StubInterpreter:
    def __init__(self, result: IntentResult):
        self.result = result
        self.call_count = 0

    def __call__(self, user_message: str) -> IntentResult:
        self.call_count += 1
        return self.result


def incomplete_exchange() -> IntentResult:
    return IntentResult(
        intent="exchange",
        product="耳机",
        reason="左边没有声音",
        missing_information=["order_id"],
    )


def test_first_message_with_missing_order_id_enters_waiting_state():
    interpreter = StubInterpreter(incomplete_exchange())

    state = process_message(
        ConversationState(),
        "我的耳机左边没声音，想换货",
        interpreter,
    )

    assert state.status == "waiting_for_information"
    assert state.intent_result == interpreter.result
    assert state.assistant_message == "请提供订单号"
    assert interpreter.call_count == 1


def test_second_message_completes_order_id():
    waiting_state = ConversationState(
        intent_result=incomplete_exchange(),
        status="waiting_for_information",
        assistant_message="请提供订单号",
    )

    state = process_message(waiting_state, "订单号是 A1001", StubInterpreter(incomplete_exchange()))

    assert state.status == "ready"
    assert state.intent_result is not None
    assert state.intent_result.order_id == "A1001"
    assert "order_id" not in state.intent_result.missing_information
    assert state.assistant_message == "信息已补全，准备查询订单 A1001"


def test_second_message_does_not_call_interpreter_again():
    interpreter = StubInterpreter(incomplete_exchange())
    waiting_state = process_message(
        ConversationState(),
        "我的耳机左边没声音，想换货",
        interpreter,
    )

    ready_state = process_message(waiting_state, "B2048", interpreter)

    assert ready_state.status == "ready"
    assert interpreter.call_count == 1


def test_invalid_order_id_keeps_waiting():
    interpreter = StubInterpreter(incomplete_exchange())
    waiting_state = ConversationState(
        intent_result=incomplete_exchange(),
        status="waiting_for_information",
    )

    state = process_message(waiting_state, "订单号是 1001", interpreter)

    assert state.status == "waiting_for_information"
    assert state.intent_result is not None
    assert state.intent_result.order_id is None
    assert state.assistant_message == "请提供订单号"
    assert interpreter.call_count == 0


def test_complete_first_message_is_ready_immediately():
    interpreter = StubInterpreter(
        IntentResult(
            intent="exchange",
            product="耳机",
            reason="左边没有声音",
            order_id="A1001",
        )
    )

    state = process_message(
        ConversationState(),
        "订单 A1001 的耳机左边没声音，想换货",
        interpreter,
    )

    assert state.status == "ready"
    assert state.intent_result is not None
    assert state.intent_result.order_id == "A1001"
    assert state.assistant_message == "信息已补全，准备查询订单 A1001"
    assert interpreter.call_count == 1
