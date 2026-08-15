from app.core.config import settings
from app.core.database.postgres import get_postgres_resolved_error_repository
from app.core.repositories.in_memory_resolved_error_repository import InMemoryResolvedErrorRepository
from app.core.repositories.resolved_error_repository import ResolvedErrorRepository


def _build_resolved_error_repository() -> ResolvedErrorRepository:
    if settings.resolved_error_backend == "postgres":
        return get_postgres_resolved_error_repository()
    return InMemoryResolvedErrorRepository()


resolved_error_store = _build_resolved_error_repository()