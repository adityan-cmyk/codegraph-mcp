from app.agents.engine import stream_resolution
from app.rag.assembler.context_assembler import assemble_incident_context
from app.schemas.context import ContextBounds
from app.schemas.incident import IncidentFingerprint


def run_incident_workflow(fingerprint: IncidentFingerprint) -> dict[str, object]:
    context = assemble_incident_context(fingerprint, ContextBounds())
    return stream_resolution(context)