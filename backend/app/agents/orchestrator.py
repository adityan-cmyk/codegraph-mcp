from app.agents.engine import generate_resolution
from app.core.tracing_service import tracing_service
from app.rag.assembler.context_assembler import assemble_incident_context
from app.schemas.context import ContextBounds
from app.schemas.incident import IncidentAnalysis, IncidentFingerprint


async def run_incident_workflow(
    fingerprint: IncidentFingerprint,
    bounds: ContextBounds | None = None,
) -> IncidentAnalysis:
    resolved_bounds = bounds or ContextBounds()

    with tracing_service.trace(
        "incident_workflow",
        metadata={"fingerprint": fingerprint.model_dump(), "bounds": resolved_bounds.model_dump()},
    ) as trace_obj:
        with tracing_service.span(
            "context_assembly",
            trace_id=getattr(trace_obj, "trace_id", None) if tracing_service.is_enabled else None,
        ) as span_obj:
            context = assemble_incident_context(fingerprint, resolved_bounds)
            if hasattr(span_obj, "update"):
                span_obj.update(
                    output={
                        "resolved_error": bool(context.get("resolved_error")),
                        "indexed_symbols": len(context.get("indexed_symbols", [])),
                        "graph_neighborhoods": len(context.get("graph_neighborhoods", [])),
                    }
                )

        with tracing_service.generation(
            "llm_resolution",
            trace_id=getattr(trace_obj, "trace_id", None) if tracing_service.is_enabled else None,
            model="oncall-assistant",
            input={"semantic_query": context.get("semantic_query", "")},
        ) as gen_obj:
            analysis = await generate_resolution(context)
            if hasattr(gen_obj, "update"):
                gen_obj.update(
                    output={"root_cause": analysis.root_cause, "patch": analysis.patch},
                    usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                )

        return analysis
