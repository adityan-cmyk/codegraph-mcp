from threading import Lock

from app.core.repositories.eval_case_repository import EvalCaseRepository
from app.schemas.eval import EvalCase


class InMemoryEvalCaseRepository(EvalCaseRepository):
    def __init__(self) -> None:
        self._cases: dict[str, EvalCase] = {}
        self._lock = Lock()

    def reset(self) -> None:
        with self._lock:
            self._cases.clear()

    def save(self, case: EvalCase) -> None:
        with self._lock:
            self._cases[case.case_id] = case

    def list_cases(self, environment: str | None = None) -> list[EvalCase]:
        with self._lock:
            cases = list(self._cases.values())
            if environment is not None:
                cases = [case for case in cases if case.environment == environment]
            return sorted(cases, key=lambda case: case.created_at or "", reverse=True)

    def get_case(self, case_id: str) -> EvalCase | None:
        with self._lock:
            return self._cases.get(case_id)
