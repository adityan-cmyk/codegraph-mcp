from app.core.config import settings
from app.core.database.postgres import get_postgres_incident_repository
from app.core.repositories.in_memory_incident_repository import InMemoryIncidentRepository
from app.core.repositories.incident_repository import IncidentRepository, SessionNotFoundError


def _build_incident_repository() -> IncidentRepository:
    if settings.incident_store_backend == "postgres":
        return get_postgres_incident_repository()
    return InMemoryIncidentRepository()


incident_session_store = _build_incident_repository()