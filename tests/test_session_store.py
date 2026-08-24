from uuid import UUID

import pytest

from schemas import ConversationState
from session_store import InMemorySessionStore, SessionNotFoundError


def test_session_store_creates_uuid_and_isolates_snapshots():
    store = InMemorySessionStore()
    first_id = store.create()
    second_id = store.create()

    assert UUID(first_id).version == 4
    assert UUID(second_id).version == 4
    assert first_id != second_id

    first_state = store.get(first_id)
    first_state.status = "cancelled"

    assert store.get(first_id).status == "new"
    assert store.get(second_id).status == "new"


def test_session_store_rejects_unknown_session():
    store = InMemorySessionStore()

    with pytest.raises(SessionNotFoundError):
        store.get("missing")

    with pytest.raises(SessionNotFoundError):
        store.save("missing", ConversationState())
