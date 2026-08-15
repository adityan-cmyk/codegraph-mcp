from threading import Lock

from app.core.repositories.resolved_error_repository import ResolvedErrorRepository
from app.schemas.incident import ResolutionPackage


class InMemoryResolvedErrorRepository(ResolvedErrorRepository):
    def __init__(self) -> None:
        self._packages: list[ResolutionPackage] = []
        self._lock = Lock()

    def reset(self) -> None:
        with self._lock:
            self._packages.clear()

    def save(self, package: ResolutionPackage) -> None:
        with self._lock:
            self._packages = [item for item in self._packages if item.fingerprint != package.fingerprint]
            self._packages.append(package)

    def list_packages(self) -> list[ResolutionPackage]:
        with self._lock:
            return list(self._packages)