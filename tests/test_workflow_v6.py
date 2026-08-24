import pytest

from chat import run_chat
from exchange_tools import InMemoryExchangeService
from order_tools import OrderRecord, query_order
from refund_tools import InMemoryRefundService
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


class CountingCreator:
    def __init__(self, creator):
        self.creator = creator
        self.calls: list[tuple[str, str]] = []

    def __call__(self, order_id: str, reason: str):
        self.calls.append((order_id, reason))
        return self.creator(order_id, reason)


def intent_result(intent: str, order_id: str = "A1001") -> IntentResult:
    return IntentResult(
        intent=intent,
        order_id=order_id,
        reason="测试原因",
    )


def refund_confirmation_state(
    lookup: CountingOrderLookup,
    exchange_creator: CountingCreator,
    refund_creator: CountingCreator,
) -> tuple[ConversationState, SequenceInterpreter]:
    interpreter = SequenceInterpreter(intent_result("refund"))
    state = process_message(
        ConversationState(),
        "订单 A1001 要退款",
        interpreter,
        lookup,
        exchange_creator,
        refund_creator,
    )
    return state, interpreter


def test_a1001_refund_is_eligible_without_creator():
    order = query_order("A1001")
    assert order is not None
    lookup = CountingOrderLookup(order)

    state = process_message(
        ConversationState(),
        "订单 A1001 要退款",
        SequenceInterpreter(intent_result("refund")),
        lookup,
    )

    assert state.status == "order_checked"
    assert state.order == order
    assert state.pending_action is None
    assert state.eligibility_reason is not None
    assert "符合演示退款条件" in state.eligibility_reason
    assert lookup.calls == ["A1001"]


@pytest.mark.parametrize(
    ("order", "order_id", "reason_fragment"),
    [
        (query_order("B2048"), "B2048", "超过 7 天演示退款期限"),
        (query_order("C3003"), "C3003", "尚未收货"),
        (
            OrderRecord(order_id="D4004", product="测试商品", status="cancelled"),
            "D4004",
            "订单已经取消",
        ),
        (
            OrderRecord(order_id="E5005", product="测试商品", status="delivered"),
            "E5005",
            "缺少收货时间",
        ),
        (None, "Z9999", "未查询到订单"),
    ],
)
def test_ineligible_refund_orders_are_rejected(order, order_id, reason_fragment):
    lookup = CountingOrderLookup(order)
    refund_service = InMemoryRefundService()
    refund_creator = CountingCreator(refund_service.create_request)

    state = process_message(
        ConversationState(),
        f"订单 {order_id} 要退款",
        SequenceInterpreter(intent_result("refund", order_id)),
        lookup,
        refund_creator=refund_creator,
    )

    assert state.status == "rejected"
    assert state.pending_action is None
    assert state.eligibility_reason is not None
    assert reason_fragment in state.eligibility_reason
    assert lookup.calls == [order_id]
    assert refund_creator.calls == []


def test_refund_is_not_created_before_confirmation():
    order = query_order("A1001")
    assert order is not None
    lookup = CountingOrderLookup(order)
    exchange_service = InMemoryExchangeService()
    refund_service = InMemoryRefundService()
    exchange_creator = CountingCreator(exchange_service.create_request)
    refund_creator = CountingCreator(refund_service.create_request)

    waiting, _ = refund_confirmation_state(
        lookup,
        exchange_creator,
        refund_creator,
    )

    assert waiting.status == "waiting_for_confirmation"
    assert waiting.pending_action == "refund"
    assert waiting.assistant_message == "订单符合退款条件，是否确认创建退款申请？"
    assert waiting.refund_request is None
    assert exchange_creator.calls == []
    assert refund_creator.calls == []


def test_refund_queries_only_after_second_turn_order_id():
    interpreter = SequenceInterpreter(IntentResult(intent="refund", reason="测试原因"))
    order = query_order("A1001")
    assert order is not None
    lookup = CountingOrderLookup(order)
    refund_service = InMemoryRefundService()
    refund_creator = CountingCreator(refund_service.create_request)

    waiting_for_order = process_message(
        ConversationState(),
        "我要退款",
        interpreter,
        lookup,
        refund_creator=refund_creator,
    )
    assert waiting_for_order.status == "waiting_for_information"
    assert lookup.calls == []

    waiting_for_confirmation = process_message(
        waiting_for_order,
        "订单号 A1001",
        interpreter,
        lookup,
        refund_creator=refund_creator,
    )

    assert waiting_for_confirmation.status == "waiting_for_confirmation"
    assert waiting_for_confirmation.pending_action == "refund"
    assert lookup.calls == ["A1001"]
    assert refund_creator.calls == []
    assert interpreter.call_count == 1


def test_refund_confirmation_creates_only_once_and_never_calls_exchange():
    order = query_order("A1001")
    assert order is not None
    lookup = CountingOrderLookup(order)
    exchange_service = InMemoryExchangeService()
    refund_service = InMemoryRefundService()
    exchange_creator = CountingCreator(exchange_service.create_request)
    refund_creator = CountingCreator(refund_service.create_request)
    waiting, interpreter = refund_confirmation_state(
        lookup,
        exchange_creator,
        refund_creator,
    )

    completed = process_message(
        waiting,
        "确认",
        interpreter,
        lookup,
        exchange_creator,
        refund_creator,
    )
    repeated = process_message(
        completed,
        "再次确认",
        interpreter,
        lookup,
        exchange_creator,
        refund_creator,
    )

    assert repeated.status == "completed"
    assert repeated.pending_action is None
    assert repeated.refund_request is not None
    assert repeated.refund_request.order_id == "A1001"
    assert repeated.exchange_request is None
    assert lookup.calls == ["A1001"]
    assert refund_creator.calls == [("A1001", "测试原因")]
    assert exchange_creator.calls == []
    assert refund_service.request_count == 1
    assert interpreter.call_count == 1


def test_ambiguous_then_cancelled_refund_never_creates_and_stays_terminal():
    order = query_order("A1001")
    assert order is not None
    lookup = CountingOrderLookup(order)
    exchange_service = InMemoryExchangeService()
    refund_service = InMemoryRefundService()
    exchange_creator = CountingCreator(exchange_service.create_request)
    refund_creator = CountingCreator(refund_service.create_request)
    waiting, interpreter = refund_confirmation_state(
        lookup,
        exchange_creator,
        refund_creator,
    )

    ambiguous = process_message(
        waiting,
        "我再考虑一下",
        interpreter,
        lookup,
        exchange_creator,
        refund_creator,
    )
    cancelled = process_message(
        ambiguous,
        "取消",
        interpreter,
        lookup,
        exchange_creator,
        refund_creator,
    )
    repeated = process_message(
        cancelled,
        "确认",
        interpreter,
        lookup,
        exchange_creator,
        refund_creator,
    )

    assert ambiguous.status == "waiting_for_confirmation"
    assert ambiguous.pending_action == "refund"
    assert ambiguous.assistant_message == "订单符合退款条件，是否确认创建退款申请？"
    assert repeated.status == "cancelled"
    assert repeated.pending_action is None
    assert repeated.assistant_message == "已取消创建退款申请"
    assert lookup.calls == ["A1001"]
    assert exchange_creator.calls == []
    assert refund_creator.calls == []
    assert interpreter.call_count == 1


def test_rejected_refund_does_not_repeat_any_tool():
    order = query_order("B2048")
    assert order is not None
    lookup = CountingOrderLookup(order)
    exchange_service = InMemoryExchangeService()
    refund_service = InMemoryRefundService()
    exchange_creator = CountingCreator(exchange_service.create_request)
    refund_creator = CountingCreator(refund_service.create_request)
    interpreter = SequenceInterpreter(intent_result("refund", "B2048"))

    rejected = process_message(
        ConversationState(),
        "订单 B2048 要退款",
        interpreter,
        lookup,
        exchange_creator,
        refund_creator,
    )
    repeated = process_message(
        rejected,
        "重新检查",
        interpreter,
        lookup,
        exchange_creator,
        refund_creator,
    )

    assert repeated.status == "rejected"
    assert lookup.calls == ["B2048"]
    assert exchange_creator.calls == []
    assert refund_creator.calls == []
    assert interpreter.call_count == 1


def test_exchange_confirmation_never_calls_refund_creator():
    exchange_service = InMemoryExchangeService()
    refund_service = InMemoryRefundService()
    exchange_creator = CountingCreator(exchange_service.create_request)
    refund_creator = CountingCreator(refund_service.create_request)
    interpreter = SequenceInterpreter(intent_result("exchange"))

    waiting = process_message(
        ConversationState(),
        "订单 A1001 要换货",
        interpreter,
        query_order,
        exchange_creator,
        refund_creator,
    )
    completed = process_message(
        waiting,
        "确认",
        interpreter,
        query_order,
        exchange_creator,
        refund_creator,
    )

    assert waiting.pending_action == "exchange"
    assert completed.status == "completed"
    assert completed.pending_action is None
    assert completed.exchange_request is not None
    assert completed.refund_request is None
    assert exchange_creator.calls == [("A1001", "测试原因")]
    assert refund_creator.calls == []


def test_refund_without_order_lookup_keeps_v4_ready_behavior():
    refund_service = InMemoryRefundService()
    refund_creator = CountingCreator(refund_service.create_request)

    state = process_message(
        ConversationState(),
        "订单 A1001 要退款",
        SequenceInterpreter(intent_result("refund")),
        refund_creator=refund_creator,
    )

    assert state.status == "ready"
    assert state.assistant_message == "信息已补全，准备处理订单 A1001 的退款"
    assert refund_creator.calls == []


def test_v5_logistics_flow_still_answers_without_calling_creators():
    exchange_service = InMemoryExchangeService()
    refund_service = InMemoryRefundService()
    exchange_creator = CountingCreator(exchange_service.create_request)
    refund_creator = CountingCreator(refund_service.create_request)

    state = process_message(
        ConversationState(),
        "订单 A1001 的物流",
        SequenceInterpreter(intent_result("logistics")),
        query_order,
        exchange_creator,
        refund_creator,
    )

    assert state.status == "answered"
    assert state.assistant_message == "订单 A1001 已经签收（3 天前签收）"
    assert exchange_creator.calls == []
    assert refund_creator.calls == []


def test_chat_runs_refund_confirmation_to_completion():
    messages = iter(["订单 A1001 要退款", "确认"])
    outputs: list[str] = []
    exchange_service = InMemoryExchangeService()
    refund_service = InMemoryRefundService()

    state = run_chat(
        input_func=lambda: next(messages),
        output_func=outputs.append,
        interpreter=SequenceInterpreter(intent_result("refund")),
        order_lookup=query_order,
        exchange_creator=exchange_service.create_request,
        refund_creator=refund_service.create_request,
    )

    assert state.status == "completed"
    assert state.refund_request is not None
    assert exchange_service.request_count == 0
    assert refund_service.request_count == 1
    assert outputs == [
        "订单符合退款条件，是否确认创建退款申请？",
        "退款申请已创建，申请编号 RF-0001",
    ]
