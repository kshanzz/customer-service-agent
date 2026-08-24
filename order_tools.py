from typing import Literal

from pydantic import BaseModel


class OrderRecord(BaseModel):
    order_id: str
    product: str
    status: Literal["delivered", "shipped", "cancelled"]
    days_since_delivery: int | None = None


class ExchangeEligibility(BaseModel):
    eligible: bool
    reason: str


class RefundEligibility(BaseModel):
    eligible: bool
    reason: str


_ORDERS = {
    "A1001": OrderRecord(
        order_id="A1001",
        product="耳机",
        status="delivered",
        days_since_delivery=3,
    ),
    "B2048": OrderRecord(
        order_id="B2048",
        product="键盘",
        status="delivered",
        days_since_delivery=15,
    ),
    "C3003": OrderRecord(
        order_id="C3003",
        product="鼠标",
        status="shipped",
        days_since_delivery=None,
    ),
}


def query_order(order_id: str) -> OrderRecord | None:
    """Return an isolated copy of an in-memory demo order."""
    order = _ORDERS.get(order_id.upper())
    if order is None:
        return None
    return order.model_copy(deep=True)


def check_exchange_eligibility(order: OrderRecord) -> ExchangeEligibility:
    """Apply the deterministic V2 demonstration exchange policy."""
    if order.status != "delivered":
        return ExchangeEligibility(
            eligible=False,
            reason="订单尚未收货，不符合演示换货条件",
        )

    if order.days_since_delivery is None:
        return ExchangeEligibility(
            eligible=False,
            reason="订单缺少收货时间，无法确认演示换货资格",
        )

    if order.days_since_delivery > 7:
        return ExchangeEligibility(
            eligible=False,
            reason=(
                f"已收货 {order.days_since_delivery} 天，超过 7 天演示换货期限"
            ),
        )

    return ExchangeEligibility(
        eligible=True,
        reason=f"已收货 {order.days_since_delivery} 天，符合演示换货条件",
    )


def check_refund_eligibility(order: OrderRecord) -> RefundEligibility:
    """Apply the deterministic V6 demonstration refund policy."""
    if order.status != "delivered":
        reason = (
            "订单已经取消，不符合演示退款条件"
            if order.status == "cancelled"
            else "订单尚未收货，不符合演示退款条件"
        )
        return RefundEligibility(eligible=False, reason=reason)

    if order.days_since_delivery is None:
        return RefundEligibility(
            eligible=False,
            reason="订单缺少收货时间，无法确认演示退款资格",
        )

    if order.days_since_delivery > 7:
        return RefundEligibility(
            eligible=False,
            reason=(
                f"已收货 {order.days_since_delivery} 天，超过 7 天演示退款期限"
            ),
        )

    return RefundEligibility(
        eligible=True,
        reason=f"已收货 {order.days_since_delivery} 天，符合演示退款条件",
    )
