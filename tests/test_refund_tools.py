from refund_tools import InMemoryRefundService


def test_create_refund_request_is_idempotent_for_same_order():
    service = InMemoryRefundService()

    first = service.create_request("a1001", "七天内退款")
    second = service.create_request("A1001", "重复申请")

    assert second is first
    assert first.request_id == "RF-0001"
    assert first.order_id == "A1001"
    assert first.reason == "七天内退款"
    assert first.status == "created"
    assert service.request_count == 1


def test_refund_services_do_not_share_state():
    first_service = InMemoryRefundService()
    second_service = InMemoryRefundService()

    first_request = first_service.create_request("A1001", "第一次会话")
    second_request = second_service.create_request("A1001", "第二次会话")

    assert first_request.reason == "第一次会话"
    assert second_request.reason == "第二次会话"
    assert first_service.request_count == 1
    assert second_service.request_count == 1
