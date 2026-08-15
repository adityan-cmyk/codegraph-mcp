from app.schemas.context import ContextBounds
from app.schemas.incident import IncidentFingerprint
from app.rag.retrieval.graph import get_blast_radius
from app.rag.retrieval.semantic import lookup_resolved_error, semantic_index


def assemble_incident_context(
    fingerprint: IncidentFingerprint,
    bounds: ContextBounds,
) -> dict[str, object]:
    semantic_query = " ".join(
        [fingerprint.service, fingerprint.panic_type, fingerprint.top_frame, fingerprint.commit_hash]
    )
    semantic_matches = semantic_index.query_chunks(semantic_query, limit=bounds.graph_depth * 2)
    resolved_error = lookup_resolved_error(fingerprint)
    related_symbols = [match.symbol_id for match in semantic_matches[: bounds.graph_depth]]
    graph_neighborhoods = [get_blast_radius(symbol_id) for symbol_id in related_symbols]
    indexed_symbols = [match.model_dump() for match in semantic_matches]

    return {
        "fingerprint": fingerprint.model_dump(),
        "semantic_query": semantic_query,
        "graph_depth": bounds.graph_depth,
        "token_budget": bounds.token_budget,
        "deployment_window": bounds.deployment_window,
        "confidence_threshold": bounds.confidence_threshold,
        "resolved_error": resolved_error,
        "primary_symbol": related_symbols[0] if related_symbols else None,
        "indexed_symbols": indexed_symbols,
        "graph_neighborhoods": graph_neighborhoods,
        "chunks": indexed_symbols,
    }