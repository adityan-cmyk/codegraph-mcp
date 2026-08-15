from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

_schema_ensured = False


class ChatRepository:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        global _schema_ensured
        if not _schema_ensured:
            self._ensure_schema()
            _schema_ensured = True

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_threads (
                        thread_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT 'New Chat',
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_messages (
                        message_id TEXT PRIMARY KEY,
                        thread_id TEXT NOT NULL REFERENCES chat_threads(thread_id) ON DELETE CASCADE,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        citations JSONB NOT NULL DEFAULT '[]'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS chat_messages_thread_created_idx ON chat_messages(thread_id, created_at)"
                )
            connection.commit()

    def create_thread(self, title: str = "New Chat") -> dict:
        thread_id = uuid4().hex
        timestamp = datetime.now(UTC)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO chat_threads (thread_id, title, created_at, updated_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (thread_id, title, timestamp, timestamp),
                )
            connection.commit()
        return {"thread_id": thread_id, "title": title, "created_at": timestamp, "updated_at": timestamp}

    def list_threads(self) -> list[dict]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT thread_id, title, created_at, updated_at FROM chat_threads ORDER BY updated_at DESC"
                )
                rows = cursor.fetchall()
        return rows

    def get_thread(self, thread_id: str) -> dict | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT thread_id, title, created_at, updated_at FROM chat_threads WHERE thread_id = %s",
                    (thread_id,),
                )
                return cursor.fetchone()

    def rename_thread(self, thread_id: str, title: str) -> dict | None:
        timestamp = datetime.now(UTC)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE chat_threads SET title = %s, updated_at = %s WHERE thread_id = %s RETURNING thread_id, title, created_at, updated_at",
                    (title, timestamp, thread_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
            connection.commit()
        return row

    def delete_thread(self, thread_id: str) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM chat_threads WHERE thread_id = %s", (thread_id,))
                deleted = cursor.rowcount > 0
            connection.commit()
        return deleted

    def add_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        citations: list[dict] | None = None,
    ) -> dict:
        message_id = uuid4().hex
        timestamp = datetime.now(UTC)
        import json
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO chat_messages (message_id, thread_id, role, content, citations, created_at)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    (message_id, thread_id, role, content, json.dumps(citations or []), timestamp),
                )
                cursor.execute(
                    "UPDATE chat_threads SET updated_at = %s WHERE thread_id = %s",
                    (timestamp, thread_id),
                )
            connection.commit()
        return {
            "message_id": message_id,
            "thread_id": thread_id,
            "role": role,
            "content": content,
            "citations": citations or [],
            "created_at": timestamp,
        }

    def get_messages(self, thread_id: str) -> list[dict]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT message_id, thread_id, role, content, citations, created_at
                    FROM chat_messages
                    WHERE thread_id = %s
                    ORDER BY created_at ASC
                    """,
                    (thread_id,),
                )
                rows = cursor.fetchall()
        import json
        result = []
        for row in rows:
            citations_data = row["citations"]
            if isinstance(citations_data, str):
                citations_data = json.loads(citations_data)
            result.append({
                "message_id": row["message_id"],
                "thread_id": row["thread_id"],
                "role": row["role"],
                "content": row["content"],
                "citations": citations_data,
                "created_at": row["created_at"],
            })
        return result
