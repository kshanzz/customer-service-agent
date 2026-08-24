from exchange_tools import InMemoryExchangeService


def test_create_request_is_idempotent_for_same_order():
    service = InMemoryExchangeService()

    first = service.create_request("A1001", "左边没有声音")
    second = service.create_request("A1001", "再次尝试创建")

    assert second is first
    assert first.request_id == second.request_id
    assert first.order_id == "A1001"
    assert first.reason == "左边没有声音"
    assert first.status == "created"
    assert service.request_count == 1


def test_services_have_independent_storage():
    first_service = InMemoryExchangeService()
    second_service = InMemoryExchangeService()

    first_service.create_request("A1001", "原因一")

    assert first_service.request_count == 1
    assert second_service.request_count == 0
