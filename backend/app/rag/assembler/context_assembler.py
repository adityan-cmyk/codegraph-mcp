from app.schemas.context import ContextBounds
from app.schemas.incident import IncidentFingerprint


def assemble_incident_context(
    fingerprint: IncidentFingerprint,
    bounds: ContextBounds,
) -> dict[str, object]:
    return {
        "fingerprint": fingerprint.model_dump(),
        "graph_depth": bounds.graph_depth,
        "token_budget": bounds.token_budget,
        "deployment_window": bounds.deployment_window,
        "confidence_threshold": bounds.confidence_threshold,
        "chunks": [],
    }