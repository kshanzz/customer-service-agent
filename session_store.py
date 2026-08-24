from uuid import uuid4

from schemas import ConversationState


class SessionNotFoundError(KeyError):
    """Raised when a requested session does not exist in this process."""


class InMemorySessionStore:
    """Single-process demo storage; not suitable for multi-worker deployment."""

    def __init__(self) -> None:
        self._states: dict[str, ConversationState] = {}

    def create(self) -> str:
        session_id = str(uuid4())
        self._states[session_id] = ConversationState()
        return session_id

    def get(self, session_id: str) -> ConversationState:
        state = self._states.get(session_id)
        if state is None:
            raise SessionNotFoundError(session_id)
        return state.model_copy(deep=True)

    def save(self, session_id: str, state: ConversationState) -> None:
        if session_id not in self._states:
            raise SessionNotFoundError(session_id)
        self._states[session_id] = state.model_copy(deep=True)
