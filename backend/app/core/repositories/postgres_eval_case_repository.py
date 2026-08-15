import json

import psycopg
from psycopg.rows import dict_row

from app.core.repositories.eval_case_repository import EvalCaseRepository
from app.schemas.eval import EvalCase


class PostgresEvalCaseRepository(EvalCaseRepository):
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
                    CREATE TABLE IF NOT EXISTS eval_cases (
                        case_id TEXT PRIMARY KEY,
                        fingerprint JSONB NOT NULL,
                        expected_root_cause TEXT NOT NULL,
                        expected_patch TEXT NOT NULL,
                        environment TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        tags JSONB NOT NULL DEFAULT '[]'::jsonb
                    )
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS eval_cases_environment_idx ON eval_cases(environment)"
                )
            connection.commit()

    def reset(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM eval_cases")
            connection.commit()

    def save(self, case: EvalCase) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO eval_cases (case_id, fingerprint, expected_root_cause, expected_patch, environment, created_at, tags)
                    VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (case_id)
                    DO UPDATE SET fingerprint = EXCLUDED.fingerprint, expected_root_cause = EXCLUDED.expected_root_cause,
                                  expected_patch = EXCLUDED.expected_patch, environment = EXCLUDED.environment,
                                  created_at = EXCLUDED.created_at, tags = EXCLUDED.tags
                    """,
                    (
                        case.case_id,
                        json.dumps(case.fingerprint.model_dump()),
                        case.expected_root_cause,
                        case.expected_patch,
                        case.environment,
                        case.created_at,
                        json.dumps(case.tags),
                    ),
                )
            connection.commit()

    def list_cases(self, environment: str | None = None) -> list[EvalCase]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if environment is not None:
                    cursor.execute(
                        "SELECT * FROM eval_cases WHERE environment = %s ORDER BY created_at DESC",
                        (environment,),
                    )
                else:
                    cursor.execute("SELECT * FROM eval_cases ORDER BY created_at DESC")
                rows = cursor.fetchall()
        return [EvalCase.model_validate(row) for row in rows]

    def get_case(self, case_id: str) -> EvalCase | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM eval_cases WHERE case_id = %s", (case_id,))
                row = cursor.fetchone()
        return EvalCase.model_validate(row) if row else None
