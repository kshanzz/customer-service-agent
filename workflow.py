import re
from collections.abc import Callable

from exchange_tools import ExchangeRequest
from order_tools import (
    OrderRecord,
    check_exchange_eligibility,
    check_refund_eligibility,
)
from refund_tools import RefundRequest
from grounded_answer import GroundedAnswer, GroundedAnswerError
from knowledge import KnowledgeHit
from schemas import ConversationState, IntentResult, KnowledgeCitation, apply_intent_field_rules


Interpreter = Callable[[str], IntentResult]
OrderLookup = Callable[[str], OrderRecord | None]
ExchangeCreator = Callable[[str, str], ExchangeRequest]
RefundCreator = Callable[[str, str], RefundRequest]
KnowledgeSearch = Callable[[str, int], list[KnowledgeHit]]
KnowledgeAnswerer = Callable[[str, list[KnowledgeHit]], GroundedAnswer]

WAITING_FOR_ORDER_MESSAGE = "请提供订单号"
CONFIRMATION_MESSAGE = "订单符合换货条件，是否确认创建换货申请？"
REFUND_CONFIRMATION_MESSAGE = "订单符合退款条件，是否确认创建退款申请？"
UNKNOWN_INTENT_MESSAGE = "暂时无法识别您的诉求，请重新说明您需要换货、退款、查询物流还是投诉。"
ORDER_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]\d{4})(?![A-Za-z0-9])")
CONFIRMATION_RESPONSES = {"确认", "是", "同意", "yes", "y"}
CANCELLATION_RESPONSES = {"取消", "否", "不同意", "no", "n"}
CONFIRMATION_MESSAGES = {
    "exchange": CONFIRMATION_MESSAGE,
    "refund": REFUND_CONFIRMATION_MESSAGE,
}
CANCELLATION_MESSAGES = {
    "exchange": "已取消创建换货申请",
    "refund": "已取消创建退款申请",
}
KNOWLEDGE_ABSTENTION_MESSAGE = "当前知识库中没有找到足够依据，请换一种方式描述或联系人工客服。"


def extract_order_id(message: str) -> str | None:
    """Extract a V1 order ID such as A1001 from free-form user text."""
    match = ORDER_ID_PATTERN.search(message)
    if match is None:
        return None
    return match.group(1).upper()


def _ready_message(intent_result: IntentResult) -> str:
    messages = {
        "exchange": f"信息已补全，准备查询订单 {intent_result.order_id} 并处理换货",
        "refund": f"信息已补全，准备处理订单 {intent_result.order_id} 的退款",
        "logistics": f"信息已补全，准备查询订单 {intent_result.order_id} 的物流",
        "complaint": "投诉信息已识别，准备转交处理",
    }
    if intent_result.intent == "unknown":
        return UNKNOWN_INTENT_MESSAGE
    return messages[intent_result.intent]


def _logistics_message(order_id: str, order: OrderRecord | None) -> str:
    """Build a deterministic, read-only logistics response."""
    if order is None:
        return f"未查询到订单 {order_id}"

    if order.status == "delivered":
        if order.days_since_delivery is not None:
            return f"订单 {order.order_id} 已经签收（{order.days_since_delivery} 天前签收）"
        return f"订单 {order.order_id} 已经签收"

    if order.status == "shipped":
        return f"订单 {order.order_id} 正在运输中"

    return f"订单 {order.order_id} 已经取消"


def _complete_intent(
    intent_result: IntentResult,
    order_lookup: OrderLookup | None,
    exchange_creator: ExchangeCreator | None,
    refund_creator: RefundCreator | None,
    knowledge_search: KnowledgeSearch | None = None,
    knowledge_answerer: KnowledgeAnswerer | None = None,
    user_message: str | None = None,
) -> ConversationState:
    if intent_result.request_kind == "information":
        if knowledge_search is None or user_message is None:
            raise RuntimeError("knowledge search is unavailable")
        hits = knowledge_search(user_message, 3)[:3]
        if not hits:
            return ConversationState(
                intent_result=intent_result,
                status="answered",
                assistant_message=KNOWLEDGE_ABSTENTION_MESSAGE,
            )
        if knowledge_answerer is None:
            raise RuntimeError("knowledge answerer is unavailable")
        grounded = knowledge_answerer(user_message, hits)
        answer = grounded.answer.strip()
        citation_ids = grounded.citation_ids
        valid_ids = {hit.citation_id for hit in hits}
        if not answer:
            raise GroundedAnswerError("grounded answer is empty")
        if not citation_ids:
            raise GroundedAnswerError("grounded answer has no citations")
        if len(set(citation_ids)) != len(citation_ids):
            raise GroundedAnswerError("grounded answer has duplicate citations")
        if not set(citation_ids).issubset(valid_ids):
            raise GroundedAnswerError("grounded answer has an unknown citation")
        citations = [
            KnowledgeCitation(
                citation_id=hit.citation_id,
                title=hit.title,
                version=hit.version,
                section=hit.section,
                source=hit.source,
            )
            for hit in hits
            if hit.citation_id in citation_ids
        ]
        return ConversationState(
            intent_result=intent_result,
            status="answered",
            assistant_message=answer,
            knowledge_citations=citations,
        )

    if intent_result.intent == "logistics" and order_lookup is not None:
        if intent_result.order_id is None:
            raise ValueError("查询物流前 order_id 不能为空")

        order = order_lookup(intent_result.order_id)
        return ConversationState(
            intent_result=intent_result,
            status="answered",
            assistant_message=_logistics_message(intent_result.order_id, order),
            order=order,
        )

    if (
        intent_result.intent not in {"exchange", "refund"}
        or order_lookup is None
    ):
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

    if intent_result.intent == "exchange":
        eligibility = check_exchange_eligibility(order)
        creator = exchange_creator
    else:
        eligibility = check_refund_eligibility(order)
        creator = refund_creator

    if eligibility.eligible and creator is not None:
        return ConversationState(
            intent_result=intent_result,
            status="waiting_for_confirmation",
            assistant_message=CONFIRMATION_MESSAGES[intent_result.intent],
            order=order,
            eligibility_reason=eligibility.reason,
            pending_action=intent_result.intent,
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
    refund_creator: RefundCreator | None = None,
    knowledge_search: KnowledgeSearch | None = None,
    knowledge_answerer: KnowledgeAnswerer | None = None,
) -> ConversationState:
    """Advance the conversation without mutating the input state."""
    if state.status == "new":
        intent_result = interpreter(user_message)
        if intent_result.request_kind == "information":
            intent_result = intent_result.model_copy(update={"missing_information": []})
        else:
            intent_result = apply_intent_field_rules(intent_result)

        if intent_result.intent == "unknown":
            return ConversationState(
                intent_result=intent_result,
                status="new",
                assistant_message=UNKNOWN_INTENT_MESSAGE,
            )

        if intent_result.missing_information:
            return ConversationState(
                intent_result=intent_result,
                status="waiting_for_information",
                assistant_message=WAITING_FOR_ORDER_MESSAGE,
            )

        return _complete_intent(
            intent_result,
            order_lookup,
            exchange_creator,
            refund_creator,
            knowledge_search,
            knowledge_answerer,
            user_message,
        )

    if state.status == "waiting_for_information":
        if state.intent_result is None:
            raise ValueError("等待补充信息时 intent_result 不能为空")

        order_id = extract_order_id(user_message)
        if order_id is None:
            return state.model_copy(
                update={"assistant_message": WAITING_FOR_ORDER_MESSAGE}
            )

        intent_result = state.intent_result.model_copy(
            update={"order_id": order_id}
        )
        intent_result = apply_intent_field_rules(intent_result)
        return _complete_intent(
            intent_result,
            order_lookup,
            exchange_creator,
            refund_creator,
            knowledge_search,
            knowledge_answerer,
            user_message,
        )

    if state.status == "waiting_for_confirmation":
        if state.pending_action is None:
            raise ValueError("等待确认时 pending_action 不能为空")

        pending_action = state.pending_action
        response = user_message.strip().lower()
        if response in CANCELLATION_RESPONSES:
            return state.model_copy(
                update={
                    "status": "cancelled",
                    "assistant_message": CANCELLATION_MESSAGES[pending_action],
                    "pending_action": None,
                }
            )

        if response not in CONFIRMATION_RESPONSES:
            return state.model_copy(
                update={"assistant_message": CONFIRMATION_MESSAGES[pending_action]}
            )

        if state.intent_result is None or state.intent_result.order_id is None:
            raise ValueError("创建售后申请前 order_id 不能为空")

        if pending_action == "exchange":
            if exchange_creator is None:
                raise RuntimeError("确认创建换货申请时必须提供 exchange_creator")

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
                    "pending_action": None,
                    "exchange_request": exchange_request,
                }
            )

        if refund_creator is None:
            raise RuntimeError("确认创建退款申请时必须提供 refund_creator")

        refund_request = refund_creator(
            state.intent_result.order_id,
            state.intent_result.reason or "用户申请退款",
        )
        return state.model_copy(
            update={
                "status": "completed",
                "assistant_message": (
                    f"退款申请已创建，申请编号 {refund_request.request_id}"
                ),
                "pending_action": None,
                "refund_request": refund_request,
            }
        )

    if state.status == "answered":
        return state.model_copy()

    return state.model_copy()
