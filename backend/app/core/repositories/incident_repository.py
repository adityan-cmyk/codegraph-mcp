from abc import ABC, abstractmethod

from app.core.state_machine import IncidentState
from app.schemas.incident import CreateIncidentRequest, IncidentEvent, IncidentSession


class SessionNotFoundError(KeyError):
    pass


class IncidentRepository(ABC):
    @abstractmethod
    def reset(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_sessions(self) -> list[IncidentSession]:
        raise NotImplementedError

    @abstractmethod
    def get_session(self, session_id: str) -> IncidentSession:
        raise NotImplementedError

    @abstractmethod
    def create_session(self, request: CreateIncidentRequest) -> IncidentSession:
        raise NotImplementedError

    @abstractmethod
    def transition_session(
        self,
        session_id: str,
        next_state: IncidentState,
        *,
        event_type: str = "state_transition",
        payload: dict[str, object] | None = None,
    ) -> IncidentSession:
        raise NotImplementedError

    @abstractmethod
    def append_event(
        self,
        session_id: str,
        event: IncidentEvent,
    ) -> None:
        """Append an event to the timeline without changing session state."""
        raise NotImplementedError