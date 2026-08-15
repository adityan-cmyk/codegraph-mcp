import json

import psycopg
from psycopg.rows import dict_row

from app.core.repositories.resolved_error_repository import ResolvedErrorRepository
from app.schemas.incident import ResolutionPackage


class PostgresResolvedErrorRepository(ResolvedErrorRepository):
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
                    CREATE TABLE IF NOT EXISTS resolved_errors (
                        fingerprint_key TEXT PRIMARY KEY,
                        fingerprint JSONB NOT NULL,
                        root_cause TEXT NOT NULL,
                        patch TEXT NOT NULL
                    )
                    """
                )
            connection.commit()

    def reset(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM resolved_errors")
            connection.commit()

    def save(self, package: ResolutionPackage) -> None:
        fingerprint_key = self._fingerprint_key(package)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO resolved_errors (fingerprint_key, fingerprint, root_cause, patch)
                    VALUES (%s, %s::jsonb, %s, %s)
                    ON CONFLICT (fingerprint_key)
                    DO UPDATE SET fingerprint = EXCLUDED.fingerprint, root_cause = EXCLUDED.root_cause, patch = EXCLUDED.patch
                    """,
                    (
                        fingerprint_key,
                        json.dumps(package.fingerprint.model_dump()),
                        package.root_cause,
                        package.patch,
                    ),
                )
            connection.commit()

    def list_packages(self) -> list[ResolutionPackage]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT fingerprint, root_cause, patch FROM resolved_errors ORDER BY fingerprint_key ASC")
                rows = cursor.fetchall()
        return [
            ResolutionPackage.model_validate(
                {
                    "fingerprint": row["fingerprint"],
                    "root_cause": row["root_cause"],
                    "patch": row["patch"],
                }
            )
            for row in rows
        ]

    @staticmethod
    def _fingerprint_key(package: ResolutionPackage) -> str:
        fingerprint = package.fingerprint
        return "|".join([fingerprint.service, fingerprint.panic_type, fingerprint.top_frame, fingerprint.commit_hash])