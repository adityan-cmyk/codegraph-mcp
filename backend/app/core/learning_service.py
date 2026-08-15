from app.core.eval_service import create_eval_case_from_resolution
from app.core.resolved_error_store import resolved_error_store
from app.rag.retrieval.semantic import semantic_index
from app.schemas.incident import IncidentSession, ResolutionPackage


def _extract_resolution_package(session: IncidentSession) -> ResolutionPackage | None:
    for event in reversed(session.timeline):
        analysis_payload = event.payload.get("analysis")
        if isinstance(analysis_payload, dict):
            root_cause = analysis_payload.get("root_cause")
            patch = analysis_payload.get("patch")
            if isinstance(root_cause, str) and isinstance(patch, str):
                return ResolutionPackage(
                    fingerprint=session.fingerprint,
                    root_cause=root_cause,
                    patch=patch,
                )
    return None


def build_resolution_package(session: IncidentSession) -> ResolutionPackage | None:
    return _extract_resolution_package(session)


def persist_resolution_package(session: IncidentSession) -> ResolutionPackage | None:
    package = build_resolution_package(session)
    if package is None:
        return None
    resolved_error_store.save(package)
    semantic_index.add_resolution_package(package)
    if session.environment == "UAT" and session.state.value == "RESOLVED":
        create_eval_case_from_resolution(session, package)
    return package


def replay_resolved_errors_from_storage() -> int:
    packages = resolved_error_store.list_packages()
    for package in packages:
        semantic_index.add_resolution_package(package)
    return len(packages)