from app.core.config import settings
from app.core.database.postgres import get_postgres_eval_case_repository
from app.core.repositories.eval_case_repository import EvalCaseRepository
from app.core.repositories.in_memory_eval_case_repository import InMemoryEvalCaseRepository


def _build_eval_case_repository() -> EvalCaseRepository:
    if settings.eval_case_backend == "postgres":
        return get_postgres_eval_case_repository()
    return InMemoryEvalCaseRepository()


eval_case_store = _build_eval_case_repository()
