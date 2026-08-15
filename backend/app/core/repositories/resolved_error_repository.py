from abc import ABC, abstractmethod

from app.schemas.incident import ResolutionPackage


class ResolvedErrorRepository(ABC):
    @abstractmethod
    def reset(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def save(self, package: ResolutionPackage) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_packages(self) -> list[ResolutionPackage]:
        raise NotImplementedError