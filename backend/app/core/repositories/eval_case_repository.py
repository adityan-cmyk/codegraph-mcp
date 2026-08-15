from abc import ABC, abstractmethod

from app.schemas.eval import EvalCase


class EvalCaseRepository(ABC):
    @abstractmethod
    def reset(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def save(self, case: EvalCase) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_cases(self, environment: str | None = None) -> list[EvalCase]:
        raise NotImplementedError

    @abstractmethod
    def get_case(self, case_id: str) -> EvalCase | None:
        raise NotImplementedError
