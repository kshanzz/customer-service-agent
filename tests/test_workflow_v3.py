from exchange_tools import InMemoryExchangeService
from order_tools import query_order
from schemas import ConversationState, IntentResult
from workflow import process_message


class StubInterpreter:
    def __init__(self, order_id: str = "A1001"):
        self.order_id = order_id
        self.call_count = 0

    def __call__(self, user_message: str) -> IntentResult:
        self.call_count += 1
        return IntentResult(
            intent="exchange",
            product="耳机",
            reason="左边没有声音",
            order_id=self.order_id,
        )


class CountingOrderLookup:
    def __init__(self):
        self.call_count = 0

    def __call__(self, order_id: str):
        self.call_count += 1
        return query_order(order_id)


class CountingExchangeCreator:
    def __init__(self):
        self.service = InMemoryExchangeService()
        self.call_count = 0

    def __call__(self, order_id: str, reason: str):
        self.call_count += 1
        return self.service.create_request(order_id, reason)


def enter_confirmation(
    lookup: CountingOrderLookup,
    creator: CountingExchangeCreator,
) -> tuple[ConversationState, StubInterpreter]:
    interpreter = StubInterpreter()
    state = process_message(
        ConversationState(),
        "订单 A1001 的耳机想换货",
        interpreter,
        lookup,
        creator,
    )
    return state, interpreter


def test_request_is_never_created_before_confirmation():
    lookup = CountingOrderLookup()
    creator = CountingExchangeCreator()

    state, _ = enter_confirmation(lookup, creator)

    assert state.status == "waiting_for_confirmation"
    assert state.assistant_message == "订单符合换货条件，是否确认创建换货申请？"
    assert state.exchange_request is None
    assert creator.call_count == 0


def test_explicit_confirmation_creates_only_once():
    lookup = CountingOrderLookup()
    creator = CountingExchangeCreator()
    waiting_state, interpreter = enter_confirmation(lookup, creator)

    completed_state = process_message(
        waiting_state,
        "确认",
        interpreter,
        lookup,
        creator,
    )
    repeated_state = process_message(
        completed_state,
        "yes",
        interpreter,
        lookup,
        creator,
    )

    assert repeated_state.status == "completed"
    assert repeated_state.exchange_request is not None
    assert repeated_state.exchange_request.order_id == "A1001"
    assert creator.call_count == 1
    assert creator.service.request_count == 1
    assert lookup.call_count == 1


def test_explicit_rejection_cancels_without_creating():
    lookup = CountingOrderLookup()
    creator = CountingExchangeCreator()
    waiting_state, interpreter = enter_confirmation(lookup, creator)

    cancelled_state = process_message(
        waiting_state,
        "不同意",
        interpreter,
        lookup,
        creator,
    )
    final_state = process_message(
        cancelled_state,
        "确认",
        interpreter,
        lookup,
        creator,
    )

    assert final_state.status == "cancelled"
    assert final_state.exchange_request is None
    assert creator.call_count == 0
    assert lookup.call_count == 1


def test_ambiguous_reply_keeps_waiting_without_creating():
    lookup = CountingOrderLookup()
    creator = CountingExchangeCreator()
    waiting_state, interpreter = enter_confirmation(lookup, creator)

    state = process_message(
        waiting_state,
        "我再想想",
        interpreter,
        lookup,
        creator,
    )

    assert state.status == "waiting_for_confirmation"
    assert state.assistant_message == "订单符合换货条件，是否确认创建换货申请？"
    assert creator.call_count == 0
    assert lookup.call_count == 1


def test_rejected_terminal_does_not_repeat_tools():
    lookup = CountingOrderLookup()
    creator = CountingExchangeCreator()
    interpreter = StubInterpreter("B2048")
    rejected_state = process_message(
        ConversationState(),
        "订单 B2048 想换货",
        interpreter,
        lookup,
        creator,
    )

    final_state = process_message(
        rejected_state,
        "确认",
        interpreter,
        lookup,
        creator,
    )

    assert final_state.status == "rejected"
    assert lookup.call_count == 1
    assert creator.call_count == 0


def test_v1_and_v2_staged_compatibility():
    interpreter = StubInterpreter()

    v1_state = process_message(
        ConversationState(),
        "订单 A1001 想换货",
        interpreter,
    )
    v2_state = process_message(
        ConversationState(),
        "订单 A1001 想换货",
        interpreter,
        query_order,
    )

    assert v1_state.status == "ready"
    assert v2_state.status == "order_checked"
