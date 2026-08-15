from datetime import UTC, datetime
from threading import Lock
from uuid import uuid4

from app.core.repositories.incident_repository import IncidentRepository, SessionNotFoundError
from app.core.state_machine import IncidentState, validate_transition
from app.schemas.incident import CreateIncidentRequest, IncidentEvent, IncidentSession


class InMemoryIncidentRepository(IncidentRepository):
    def __init__(self) -> None:
        self._sessions: dict[str, IncidentSession] = {}
        self._lock = Lock()

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()

    def list_sessions(self) -> list[IncidentSession]:
        with self._lock:
            return sorted(self._sessions.values(), key=lambda session: session.updated_at, reverse=True)

    def get_session(self, session_id: str) -> IncidentSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            return session

    def create_session(self, request: CreateIncidentRequest) -> IncidentSession:
        timestamp = datetime.now(UTC)
        session_id = uuid4().hex
        created_event = IncidentEvent(
            event_id=uuid4().hex,
            session_id=session_id,
            event_type="session_created",
            state=IncidentState.CREATED,
            payload={"source": request.source},
            created_at=timestamp,
        )
        session = IncidentSession(
            session_id=session_id,
            fingerprint=request.fingerprint,
            environment=request.environment,
            build_id=request.build_id,
            raw_log=request.raw_log,
            source=request.source,
            state=IncidentState.CREATED,
            created_at=timestamp,
            updated_at=timestamp,
            timeline=[created_event],
        )

        with self._lock:
            self._sessions[session_id] = session

        return session

    def transition_session(
        self,
        session_id: str,
        next_state: IncidentState,
        *,
        event_type: str = "state_transition",
        payload: dict[str, object] | None = None,
    ) -> IncidentSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)

            validate_transition(session.state, next_state)
            timestamp = datetime.now(UTC)
            event = IncidentEvent(
                event_id=uuid4().hex,
                session_id=session_id,
                event_type=event_type,
                state=next_state,
                payload=payload or {},
                created_at=timestamp,
            )
            updated_session = session.model_copy(
                update={
                    "state": next_state,
                    "updated_at": timestamp,
                    "timeline": [*session.timeline, event],
                }
            )
            self._sessions[session_id] = updated_session
            return updated_session

    def append_event(
        self,
        session_id: str,
        event: IncidentEvent,
    ) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)

            updated_session = session.model_copy(
                update={
                    "updated_at": datetime.now(UTC),
                    "timeline": [*session.timeline, event],
                }
            )
            self._sessions[session_id] = updated_session