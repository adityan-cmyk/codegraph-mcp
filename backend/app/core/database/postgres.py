from app.core.config import settings
from app.core.repositories.postgres_incident_repository import PostgresIncidentRepository
from app.core.repositories.postgres_index_metadata_repository import PostgresIndexMetadataRepository
from app.core.repositories.postgres_resolved_error_repository import PostgresResolvedErrorRepository
from app.core.repositories.postgres_eval_case_repository import PostgresEvalCaseRepository


def get_postgres_incident_repository() -> PostgresIncidentRepository:
    return PostgresIncidentRepository(settings.postgres_dsn)


def get_postgres_index_metadata_repository() -> PostgresIndexMetadataRepository:
    return PostgresIndexMetadataRepository(settings.postgres_dsn)


def get_postgres_resolved_error_repository() -> PostgresResolvedErrorRepository:
    return PostgresResolvedErrorRepository(settings.postgres_dsn)


def get_postgres_eval_case_repository() -> PostgresEvalCaseRepository:
    return PostgresEvalCaseRepository(settings.postgres_dsn)