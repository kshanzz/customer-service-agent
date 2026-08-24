import json

from schemas import ConversationState, IntentResult
from tracing import run_traced_message
from order_tools import OrderRecord
from exchange_tools import InMemoryExchangeService
from refund_tools import InMemoryRefundService
import pytest


class SequenceInterpreter:
    def __init__(self, *results):
        self._results = list(results)
        self.calls: list[str] = []
        self._index = 0

    def __call__(self, message: str) -> IntentResult:
        self.calls.append(message)
        result = self._results[self._index]
        self._index += 1
        return result


class FailingInterpreter:
    def __init__(self, error: Exception):
        self.calls: list[str] = []
        self.error = error

    def __call__(self, message: str) -> IntentResult:
        self.calls.append(message)
        raise self.error


def test_traced_message_records_state_transition_without_sensitive_payload():
    state = ConversationState()
    exchange_service = InMemoryExchangeService()
    refund_service = InMemoryRefundService()
    order = {
        "A1001": OrderRecord(
            order_id="A1001",
            product="耳机",
            status="shipped",
            days_since_delivery=None,
        )
    }

    def lookup(order_id: str) -> OrderRecord | None:
        return order[order_id]

    state_after, trace = run_traced_message(
        state,
        "查询订单 A1001 的物流",
        SequenceInterpreter(IntentResult(intent="logistics", order_id="A1001")),
        lookup,
        exchange_service.create_request,
        refund_service.create_request,
    )

    assert trace.success is True
    assert trace.state_before["status"] == "new"
    assert trace.state_after["status"] == "answered"
    assert trace.events
    assert any(
        event.event_type == "state_transition" and event.component == "workflow"
        for event in trace.events
    )
    payload = json.dumps(trace.model_dump(exclude_none=True), ensure_ascii=False)
    assert "A1001" not in payload
    assert "EX-" not in payload
    assert "RF-" not in payload
    assert "secret" not in payload
    assert state_after.status == "answered"


def test_traced_message_counts_tools_and_interpreter_calls():
    state = ConversationState()
    exchange_service = InMemoryExchangeService()
    refund_service = InMemoryRefundService()

    def lookup(order_id: str) -> OrderRecord | None:
        return OrderRecord(
            order_id=order_id,
            product="耳机",
            status="delivered",
            days_since_delivery=3,
        )

    interpreter = SequenceInterpreter(
        IntentResult(intent="exchange", reason="耳机故障")
    )

    waiting, trace_waiting = run_traced_message(
        state,
        "我要换货",
        interpreter,
        lookup,
        exchange_service.create_request,
        refund_service.create_request,
    )
    assert waiting.status == "waiting_for_information"
    assert any(event.component == "interpreter" for event in trace_waiting.events)
    assert trace_waiting.state_after["status"] == "waiting_for_information"

    ready, trace_ready = run_traced_message(
        waiting,
        "订单号是 A1001",
        interpreter,
        lookup,
        exchange_service.create_request,
        refund_service.create_request,
    )
    assert ready.status == "waiting_for_confirmation"
    assert len(interpreter.calls) == 1
    confirmation, trace_confirmation = run_traced_message(
        ready,
        "确认",
        interpreter,
        lookup,
        exchange_service.create_request,
        refund_service.create_request,
    )

    assert confirmation.status == "completed"
    assert exchange_service.request_count == 1
    assert refund_service.request_count == 0
    assert len(interpreter.calls) == 1
    # two tool calls: order lookup once in ready turn, exchange creator once on confirmation.
    assert len([e for e in trace_ready.events if e.component == "order_lookup"]) == 1
    assert len([e for e in trace_confirmation.events if e.component == "exchange_creator"]) == 1


def test_traced_message_failure_keeps_original_state_and_trace_includes_error():
    state = ConversationState()
    sentry = []
    interpreter = FailingInterpreter(RuntimeError("api key missing"))

    with pytest.raises(RuntimeError):
        run_traced_message(
            state,
            "help",
            interpreter,
            None,
            None,
            None,
            trace_sink=sentry.append,
        )

    assert len(sentry) == 1
    trace = sentry[0]
    assert not trace.success
    assert trace.error_type == "RuntimeError"
    assert trace.state_before == trace.state_after


def test_trace_sink_failure_does_not_break_runtime():
    state = ConversationState(
        intent_result=IntentResult(intent="complaint", reason="测试")
    )

    def sink(_):
        raise AssertionError("sink failed")

    next_state, _ = run_traced_message(
        state,
        "我要投诉",
        SequenceInterpreter(IntentResult(intent="complaint")),
        trace_sink=sink,
    )
    assert next_state.status == "ready"
