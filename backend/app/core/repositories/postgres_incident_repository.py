import json
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from app.core.repositories.incident_repository import IncidentRepository, SessionNotFoundError
from app.core.state_machine import IncidentState, validate_transition
from app.schemas.incident import CreateIncidentRequest, IncidentEvent, IncidentFingerprint, IncidentSession


class PostgresIncidentRepository(IncidentRepository):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._ensure_schema()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS incident_sessions (
                        session_id TEXT PRIMARY KEY,
                        fingerprint JSONB NOT NULL,
                        environment TEXT NOT NULL,
                        build_id TEXT NOT NULL,
                        raw_log TEXT NOT NULL,
                        source TEXT NOT NULL,
                        state TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS incident_events (
                        event_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES incident_sessions(session_id) ON DELETE CASCADE,
                        event_type TEXT NOT NULL,
                        state TEXT NOT NULL,
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS incident_events_session_created_idx ON incident_events(session_id, created_at)"
                )
            connection.commit()

    def reset(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM incident_events")
                cursor.execute("DELETE FROM incident_sessions")
            connection.commit()

    def list_sessions(self) -> list[IncidentSession]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM incident_sessions ORDER BY updated_at DESC"
                )
                rows = cursor.fetchall()
                return [self._hydrate_session(connection, row) for row in rows]

    def get_session(self, session_id: str) -> IncidentSession:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM incident_sessions WHERE session_id = %s", (session_id,))
                row = cursor.fetchone()
                if row is None:
                    raise SessionNotFoundError(session_id)
                return self._hydrate_session(connection, row)

    def create_session(self, request: CreateIncidentRequest) -> IncidentSession:
        timestamp = datetime.now(UTC)
        session_id = uuid4().hex
        event_id = uuid4().hex

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO incident_sessions (
                        session_id, fingerprint, environment, build_id, raw_log, source, state, created_at, updated_at
                    ) VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        session_id,
                        json.dumps(request.fingerprint.model_dump()),
                        request.environment,
                        request.build_id,
                        request.raw_log,
                        request.source,
                        IncidentState.CREATED.value,
                        timestamp,
                        timestamp,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO incident_events (event_id, session_id, event_type, state, payload, created_at)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    (
                        event_id,
                        session_id,
                        "session_created",
                        IncidentState.CREATED.value,
                        json.dumps({"source": request.source}),
                        timestamp,
                    ),
                )
            connection.commit()

            session_row = {
                "session_id": session_id,
                "fingerprint": request.fingerprint.model_dump(),
                "environment": request.environment,
                "build_id": request.build_id,
                "raw_log": request.raw_log,
                "source": request.source,
                "state": IncidentState.CREATED.value,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            return self._hydrate_session(connection, session_row)

    def transition_session(
        self,
        session_id: str,
        next_state: IncidentState,
        *,
        event_type: str = "state_transition",
        payload: dict[str, object] | None = None,
    ) -> IncidentSession:
        timestamp = datetime.now(UTC)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM incident_sessions WHERE session_id = %s FOR UPDATE", (session_id,))
                row = cursor.fetchone()
                if row is None:
                    raise SessionNotFoundError(session_id)

                current_state = IncidentState(row["state"])
                validate_transition(current_state, next_state)

                cursor.execute(
                    "UPDATE incident_sessions SET state = %s, updated_at = %s WHERE session_id = %s",
                    (next_state.value, timestamp, session_id),
                )
                cursor.execute(
                    """
                    INSERT INTO incident_events (event_id, session_id, event_type, state, payload, created_at)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    (
                        uuid4().hex,
                        session_id,
                        event_type,
                        next_state.value,
                        json.dumps(payload or {}),
                        timestamp,
                    ),
                )
            connection.commit()

            row["state"] = next_state.value
            row["updated_at"] = timestamp
            return self._hydrate_session(connection, row)

    def append_event(
        self,
        session_id: str,
        event: IncidentEvent,
    ) -> None:
        timestamp = datetime.now(UTC)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT session_id FROM incident_sessions WHERE session_id = %s", (session_id,))
                if cursor.fetchone() is None:
                    raise SessionNotFoundError(session_id)

                cursor.execute(
                    """
                    INSERT INTO incident_events (event_id, session_id, event_type, state, payload, created_at)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    (
                        event.event_id,
                        session_id,
                        event.event_type,
                        event.state.value,
                        json.dumps(event.payload),
                        event.created_at,
                    ),
                )
                cursor.execute(
                    "UPDATE incident_sessions SET updated_at = %s WHERE session_id = %s",
                    (timestamp, session_id),
                )
            connection.commit()

    def _hydrate_session(self, connection: psycopg.Connection, row: dict[str, object]) -> IncidentSession:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM incident_events WHERE session_id = %s ORDER BY created_at ASC",
                (row["session_id"],),
            )
            events = cursor.fetchall()

        fingerprint_payload = row["fingerprint"]
        if isinstance(fingerprint_payload, str):
            fingerprint_payload = json.loads(fingerprint_payload)

        timeline = [
            IncidentEvent(
                event_id=event["event_id"],
                session_id=event["session_id"],
                event_type=event["event_type"],
                state=IncidentState(event["state"]),
                payload=event["payload"] if isinstance(event["payload"], dict) else json.loads(event["payload"]),
                created_at=event["created_at"],
            )
            for event in events
        ]

        return IncidentSession(
            session_id=str(row["session_id"]),
            fingerprint=IncidentFingerprint.model_validate(fingerprint_payload),
            environment=str(row["environment"]),
            build_id=str(row["build_id"]),
            raw_log=str(row["raw_log"]),
            source=str(row["source"]),
            state=IncidentState(str(row["state"])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            timeline=timeline,
        )