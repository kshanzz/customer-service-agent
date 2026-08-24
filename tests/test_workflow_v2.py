import pytest

from order_tools import query_order
from schemas import ConversationState, IntentResult
from workflow import process_message


class StubInterpreter:
    def __init__(self, order_id: str | None):
        self.order_id = order_id
        self.call_count = 0

    def __call__(self, user_message: str) -> IntentResult:
        self.call_count += 1
        return IntentResult(
            intent="exchange",
            product="演示商品",
            order_id=self.order_id,
            missing_information=[] if self.order_id else ["order_id"],
        )


class CountingOrderLookup:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, order_id: str):
        self.calls.append(order_id)
        return query_order(order_id)


@pytest.mark.parametrize(
    ("order_id", "expected_status", "reason_fragment"),
    [
        ("A1001", "order_checked", "符合演示换货条件"),
        ("B2048", "rejected", "超过 7 天演示换货期限"),
        ("C3003", "rejected", "尚未收货"),
        ("D4004", "rejected", "未查询到订单"),
    ],
)
def test_complete_exchange_queries_order_and_checks_eligibility(
    order_id,
    expected_status,
    reason_fragment,
):
    lookup = CountingOrderLookup()

    state = process_message(
        ConversationState(),
        f"订单 {order_id} 想换货",
        StubInterpreter(order_id),
        lookup,
    )

    assert state.status == expected_status
    assert state.eligibility_reason is not None
    assert reason_fragment in state.eligibility_reason
    assert lookup.calls == [order_id]
    if order_id == "D4004":
        assert state.order is None
    else:
        assert state.order is not None
        assert state.order.order_id == order_id


def test_missing_order_id_does_not_call_order_lookup():
    lookup = CountingOrderLookup()

    state = process_message(
        ConversationState(),
        "我的耳机想换货",
        StubInterpreter(None),
        lookup,
    )

    assert state.status == "waiting_for_information"
    assert lookup.calls == []


def test_order_lookup_is_called_only_once_across_two_turns():
    interpreter = StubInterpreter(None)
    lookup = CountingOrderLookup()
    waiting_state = process_message(
        ConversationState(),
        "我的耳机想换货",
        interpreter,
        lookup,
    )

    checked_state = process_message(
        waiting_state,
        "A1001",
        interpreter,
        lookup,
    )
    final_state = process_message(
        checked_state,
        "再次检查",
        interpreter,
        lookup,
    )

    assert final_state.status == "order_checked"
    assert lookup.calls == ["A1001"]
    assert interpreter.call_count == 1
