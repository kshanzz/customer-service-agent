from collections.abc import Callable

from interpreter import interpret_intent
from schemas import ConversationState, IntentResult
from workflow import process_message


InputFunction = Callable[[], str]
OutputFunction = Callable[[str], None]
Interpreter = Callable[[str], IntentResult]


def run_chat(
    input_func: InputFunction = input,
    output_func: OutputFunction = print,
    interpreter: Interpreter = interpret_intent,
) -> ConversationState:
    """Run one conversation and return its final state."""
    state = ConversationState()

    while state.status != "ready":
        user_message = input_func()
        if user_message.strip() == "/exit":
            break

        state = process_message(state, user_message, interpreter)
        if state.assistant_message is not None:
            output_func(state.assistant_message)

    return state


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(override=False)
    run_chat()
