from collections.abc import Callable

from exchange_tools import ExchangeRequest, InMemoryExchangeService
from interpreter import interpret_intent
from order_tools import OrderRecord, query_order
from schemas import ConversationState, IntentResult
from workflow import process_message


InputFunction = Callable[[], str]
OutputFunction = Callable[[str], None]
Interpreter = Callable[[str], IntentResult]
OrderLookup = Callable[[str], OrderRecord | None]
ExchangeCreator = Callable[[str, str], ExchangeRequest]

TERMINAL_STATUSES = {
    "ready",
    "order_checked",
    "completed",
    "cancelled",
    "rejected",
}


def run_chat(
    input_func: InputFunction = input,
    output_func: OutputFunction = print,
    interpreter: Interpreter = interpret_intent,
    order_lookup: OrderLookup | None = None,
    exchange_creator: ExchangeCreator | None = None,
) -> ConversationState:
    """Run one conversation and return its final state."""
    state = ConversationState()

    while state.status not in TERMINAL_STATUSES:
        user_message = input_func()
        if user_message.strip() == "/exit":
            break

        state = process_message(
            state,
            user_message,
            interpreter,
            order_lookup,
            exchange_creator,
        )
        if state.assistant_message is not None:
            output_func(state.assistant_message)

    return state


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(override=False)
    exchange_service = InMemoryExchangeService()
    run_chat(
        order_lookup=query_order,
        exchange_creator=exchange_service.create_request,
    )
