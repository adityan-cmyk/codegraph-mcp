from app.core.config import settings
from app.core.database.postgres import get_postgres_index_metadata_repository
from app.core.repositories.in_memory_index_metadata_repository import InMemoryIndexMetadataRepository
from app.core.repositories.index_metadata_repository import IndexMetadataRepository


def _build_index_metadata_repository() -> IndexMetadataRepository:
    if settings.index_metadata_backend == "postgres":
        return get_postgres_index_metadata_repository()
    return InMemoryIndexMetadataRepository()


index_metadata_store = _build_index_metadata_repository()