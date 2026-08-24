import pytest

from order_tools import check_exchange_eligibility, query_order


def test_query_known_order():
    order = query_order("A1001")

    assert order is not None
    assert order.order_id == "A1001"
    assert order.product == "耳机"
    assert order.status == "delivered"
    assert order.days_since_delivery == 3


def test_query_unknown_order():
    assert query_order("D4004") is None


@pytest.mark.parametrize(
    ("order_id", "eligible", "reason_fragment"),
    [
        ("A1001", True, "符合演示换货条件"),
        ("B2048", False, "超过 7 天演示换货期限"),
        ("C3003", False, "尚未收货"),
    ],
)
def test_exchange_eligibility(order_id, eligible, reason_fragment):
    order = query_order(order_id)
    assert order is not None

    result = check_exchange_eligibility(order)

    assert result.eligible is eligible
    assert reason_fragment in result.reason
