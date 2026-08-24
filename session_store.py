from uuid import uuid4

from schemas import ConversationState


class SessionNotFoundError(KeyError):
    """Raised when a requested session does not exist in this process."""


class SessionConcurrencyError(RuntimeError):
    """Raised when a session was modified concurrently and a write is stale."""


class InMemorySessionStore:
    """Single-process demo storage; not suitable for multi-worker deployment."""

    def __init__(self) -> None:
        self._states: dict[str, ConversationState] = {}
        self._versions: dict[str, int] = {}

    def create(self) -> str:
        session_id = str(uuid4())
        self._states[session_id] = ConversationState()
        self._versions[session_id] = 0
        return session_id

    def get(self, session_id: str) -> ConversationState:
        state = self._states.get(session_id)
        if state is None:
            raise SessionNotFoundError(session_id)
        return state.model_copy(deep=True)

    def get_with_version(self, session_id: str) -> tuple[ConversationState, int]:
        state = self._states.get(session_id)
        if state is None:
            raise SessionNotFoundError(session_id)
        return state.model_copy(deep=True), self._versions[session_id]

    def save(
        self,
        session_id: str,
        state: ConversationState,
        *,
        expected_version: int | None = None,
    ) -> None:
        if session_id not in self._states:
            raise SessionNotFoundError(session_id)
        if (
            expected_version is not None
            and expected_version != self._versions[session_id]
        ):
            raise SessionConcurrencyError("Session was updated concurrently")
        self._states[session_id] = state.model_copy(deep=True)
        self._versions[session_id] += 1
