import sqlite3
from pathlib import Path
from uuid import uuid4

from exchange_tools import ExchangeRequest
from refund_tools import RefundRequest
from schemas import ConversationState
from session_store import SessionConcurrencyError, SessionNotFoundError


def _normalize_order_id(order_id: str) -> str:
    return order_id.upper()


class _SqliteBase:
    def __init__(
        self,
        db_path: str,
        *,
        busy_timeout_ms: int = 5_000,
        enable_wal: bool = True,
    ) -> None:
        self.db_path = db_path
        self.busy_timeout_ms = busy_timeout_ms
        self.enable_wal = enable_wal

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(path),
            timeout=self.busy_timeout_ms / 1000,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        if self.enable_wal:
            connection.execute("PRAGMA journal_mode = WAL")
        return connection


class SQLiteSessionStore(_SqliteBase):
    """Persistent single-worker session storage."""

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def create(self) -> str:
        session_id = self._create_initial_session()
        return session_id

    def _create_initial_session(self) -> str:
        while True:
            connection = self._connect()
            try:
                session_id = str(uuid4())
                with connection:
                    connection.execute(
                        """
                        INSERT INTO sessions (session_id, state_json, version)
                        VALUES (?, ?, 0)
                        """,
                        (session_id, ConversationState().model_dump_json()),
                    )
                return session_id
            except sqlite3.IntegrityError:
                continue
            finally:
                connection.close()

    def get(self, session_id: str) -> ConversationState:
        state, _ = self.get_with_version(session_id)
        return state

    def get_with_version(self, session_id: str) -> tuple[ConversationState, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state_json, version
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()

        if row is None:
            raise SessionNotFoundError(session_id)

        state = ConversationState.model_validate_json(row["state_json"])
        return state.model_copy(deep=True), int(row["version"])

    def save(
        self,
        session_id: str,
        state: ConversationState,
        *,
        expected_version: int | None = None,
    ) -> None:
        state_json = state.model_dump_json()
        with self._connect() as connection:
            with connection:
                if expected_version is None:
                    cursor = connection.execute(
                        """
                        UPDATE sessions
                        SET state_json = ?, version = version + 1, updated_at = CURRENT_TIMESTAMP
                        WHERE session_id = ?
                        """,
                        (state_json, session_id),
                    )
                    if cursor.rowcount == 0:
                        raise SessionNotFoundError(session_id)
                    return

                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET state_json = ?, version = version + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = ? AND version = ?
                    """,
                    (state_json, session_id, expected_version),
                )
                if cursor.rowcount == 0:
                    exists = connection.execute(
                        """
                        SELECT 1
                        FROM sessions
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    if exists is None:
                        raise SessionNotFoundError(session_id)
                    raise SessionConcurrencyError("Session was updated concurrently")


class SQLiteExchangeService(_SqliteBase):
    """Persistent idempotent exchange request storage."""

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS exchange_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL DEFAULT '',
                    order_id TEXT NOT NULL UNIQUE,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ("created")),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def create_request(self, order_id: str, reason: str) -> ExchangeRequest:
        normalized_order_id = _normalize_order_id(order_id)
        with self._connect() as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO exchange_requests (order_id, reason, status)
                    VALUES (?, ?, ?)
                    ON CONFLICT(order_id) DO NOTHING
                    """,
                    (normalized_order_id, reason, "created"),
                )

                if cursor.rowcount == 0:
                    existing = connection.execute(
                        """
                        SELECT request_id, order_id, reason, status
                        FROM exchange_requests
                        WHERE order_id = ?
                        """,
                        (normalized_order_id,),
                    ).fetchone()
                    if existing is None:
                        raise RuntimeError("Failed to read existing exchange request")
                    return self._row_to_request(existing)

                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "Failed to insert exchange request: unexpected insert rowcount"
                    )

                request_id = f"EX-{cursor.lastrowid:04d}"
                connection.execute(
                    """
                    UPDATE exchange_requests
                    SET request_id = ?
                    WHERE id = ?
                    """,
                    (request_id, cursor.lastrowid),
                )
                inserted = connection.execute(
                    """
                    SELECT request_id, order_id, reason, status
                    FROM exchange_requests
                    WHERE id = ?
                    """,
                    (cursor.lastrowid,),
                ).fetchone()
                if inserted is None:
                    raise RuntimeError("Failed to read inserted exchange request")
                return self._row_to_request(inserted)

    @property
    def request_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS request_count FROM exchange_requests"
            ).fetchone()
        assert row is not None
        return int(row["request_count"])

    @staticmethod
    def _row_to_request(row: sqlite3.Row) -> ExchangeRequest:
        return ExchangeRequest.model_validate(
            {
                "request_id": row["request_id"],
                "order_id": row["order_id"],
                "reason": row["reason"],
                "status": row["status"],
            }
        )


class SQLiteRefundService(_SqliteBase):
    """Persistent idempotent refund request storage."""

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS refund_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL DEFAULT '',
                    order_id TEXT NOT NULL UNIQUE,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ("created")),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def create_request(self, order_id: str, reason: str) -> RefundRequest:
        normalized_order_id = _normalize_order_id(order_id)
        with self._connect() as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO refund_requests (order_id, reason, status)
                    VALUES (?, ?, ?)
                    ON CONFLICT(order_id) DO NOTHING
                    """,
                    (normalized_order_id, reason, "created"),
                )

                if cursor.rowcount == 0:
                    existing = connection.execute(
                        """
                        SELECT request_id, order_id, reason, status
                        FROM refund_requests
                        WHERE order_id = ?
                        """,
                        (normalized_order_id,),
                    ).fetchone()
                    if existing is None:
                        raise RuntimeError("Failed to read existing refund request")
                    return self._row_to_request(existing)

                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "Failed to insert refund request: unexpected insert rowcount"
                    )

                request_id = f"RF-{cursor.lastrowid:04d}"
                connection.execute(
                    """
                    UPDATE refund_requests
                    SET request_id = ?
                    WHERE id = ?
                    """,
                    (request_id, cursor.lastrowid),
                )
                inserted = connection.execute(
                    """
                    SELECT request_id, order_id, reason, status
                    FROM refund_requests
                    WHERE id = ?
                    """,
                    (cursor.lastrowid,),
                ).fetchone()
                if inserted is None:
                    raise RuntimeError("Failed to read inserted refund request")
                return self._row_to_request(inserted)

    @property
    def request_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS request_count FROM refund_requests"
            ).fetchone()
        assert row is not None
        return int(row["request_count"])

    @staticmethod
    def _row_to_request(row: sqlite3.Row) -> RefundRequest:
        return RefundRequest.model_validate(
            {
                "request_id": row["request_id"],
                "order_id": row["order_id"],
                "reason": row["reason"],
                "status": row["status"],
            }
        )
