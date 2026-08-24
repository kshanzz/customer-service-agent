import re
from collections.abc import Callable

from exchange_tools import ExchangeRequest
from order_tools import OrderRecord, check_exchange_eligibility
from schemas import ConversationState, IntentResult


Interpreter = Callable[[str], IntentResult]
OrderLookup = Callable[[str], OrderRecord | None]
ExchangeCreator = Callable[[str, str], ExchangeRequest]

WAITING_FOR_ORDER_MESSAGE = "请提供订单号"
CONFIRMATION_MESSAGE = "订单符合换货条件，是否确认创建换货申请？"
ORDER_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]\d{4})(?![A-Za-z0-9])")
CONFIRMATION_RESPONSES = {"确认", "是", "同意", "yes", "y"}
CANCELLATION_RESPONSES = {"取消", "否", "不同意", "no", "n"}


def extract_order_id(message: str) -> str | None:
    """Extract a V1 order ID such as A1001 from free-form user text."""
    match = ORDER_ID_PATTERN.search(message)
    if match is None:
        return None
    return match.group(1).upper()


def _ready_message(intent_result: IntentResult) -> str:
    if intent_result.order_id:
        return f"信息已补全，准备查询订单 {intent_result.order_id}"
    return "信息已补全，准备处理"


def _complete_intent(
    intent_result: IntentResult,
    order_lookup: OrderLookup | None,
    exchange_creator: ExchangeCreator | None,
) -> ConversationState:
    if intent_result.intent != "exchange" or order_lookup is None:
        return ConversationState(
            intent_result=intent_result,
            status="ready",
            assistant_message=_ready_message(intent_result),
        )

    if intent_result.order_id is None:
        raise ValueError("查询订单前 order_id 不能为空")

    order = order_lookup(intent_result.order_id)
    if order is None:
        reason = f"未查询到订单 {intent_result.order_id}"
        return ConversationState(
            intent_result=intent_result,
            status="rejected",
            assistant_message=reason,
            eligibility_reason=reason,
        )

    eligibility = check_exchange_eligibility(order)
    if eligibility.eligible and exchange_creator is not None:
        return ConversationState(
            intent_result=intent_result,
            status="waiting_for_confirmation",
            assistant_message=CONFIRMATION_MESSAGE,
            order=order,
            eligibility_reason=eligibility.reason,
        )

    return ConversationState(
        intent_result=intent_result,
        status="order_checked" if eligibility.eligible else "rejected",
        assistant_message=f"订单 {order.order_id}：{eligibility.reason}",
        order=order,
        eligibility_reason=eligibility.reason,
    )


def process_message(
    state: ConversationState,
    user_message: str,
    interpreter: Interpreter,
    order_lookup: OrderLookup | None = None,
    exchange_creator: ExchangeCreator | None = None,
) -> ConversationState:
    """Advance the conversation without mutating the input state."""
    if state.status == "new":
        intent_result = interpreter(user_message)
        if not intent_result.order_id:
            return ConversationState(
                intent_result=intent_result,
                status="waiting_for_information",
                assistant_message=WAITING_FOR_ORDER_MESSAGE,
            )

        return _complete_intent(intent_result, order_lookup, exchange_creator)

    if state.status == "waiting_for_information":
        if state.intent_result is None:
            raise ValueError("等待补充信息时 intent_result 不能为空")

        order_id = extract_order_id(user_message)
        if order_id is None:
            return state.model_copy(
                update={"assistant_message": WAITING_FOR_ORDER_MESSAGE}
            )

        intent_result = state.intent_result.model_copy(
            update={
                "order_id": order_id,
                "missing_information": [
                    field
                    for field in state.intent_result.missing_information
                    if field != "order_id"
                ],
            }
        )
        return _complete_intent(intent_result, order_lookup, exchange_creator)

    if state.status == "waiting_for_confirmation":
        response = user_message.strip().lower()
        if response in CANCELLATION_RESPONSES:
            return state.model_copy(
                update={
                    "status": "cancelled",
                    "assistant_message": "已取消创建换货申请",
                }
            )

        if response not in CONFIRMATION_RESPONSES:
            return state.model_copy(update={"assistant_message": CONFIRMATION_MESSAGE})

        if exchange_creator is None:
            raise RuntimeError("确认创建换货申请时必须提供 exchange_creator")
        if state.intent_result is None or state.intent_result.order_id is None:
            raise ValueError("创建换货申请前 order_id 不能为空")

        exchange_request = exchange_creator(
            state.intent_result.order_id,
            state.intent_result.reason or "用户申请换货",
        )
        return state.model_copy(
            update={
                "status": "completed",
                "assistant_message": (
                    f"换货申请已创建，申请编号 {exchange_request.request_id}"
                ),
                "exchange_request": exchange_request,
            }
        )

    return state.model_copy()
