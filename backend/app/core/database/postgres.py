from app.core.config import settings
from app.core.repositories.postgres_index_metadata_repository import PostgresIndexMetadataRepository


def get_postgres_index_metadata_repository() -> PostgresIndexMetadataRepository:
    return PostgresIndexMetadataRepository(settings.postgres_dsn)
