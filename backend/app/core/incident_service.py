import time

from app.agents.orchestrator import run_incident_workflow
from app.api.websockets.manager import incident_websocket_manager
from app.core.incident_store import SessionNotFoundError, incident_session_store
from app.core.learning_service import build_resolution_package, persist_resolution_package
from app.core.metrics import metrics_collector
from app.core.state_machine import IncidentState
from app.core.timeline_service import log_analysis_complete, log_chat_message, log_user_action
from app.schemas.context import ContextBounds
from app.schemas.incident import (
    AnalyzeIncidentRequest,
    AnalyzeIncidentResponse,
    CreateIncidentRequest,
    IncidentSession,
)


class IncidentService:
    async def create_incident(self, payload: CreateIncidentRequest) -> IncidentSession:
        session = incident_session_store.create_session(payload)
        await incident_websocket_manager.broadcast_session(session)
        metrics_collector.record_incident_created()
        return session

    def list_incidents(self) -> list[IncidentSession]:
        return incident_session_store.list_sessions()

    def get_incident(self, session_id: str) -> IncidentSession:
        return incident_session_store.get_session(session_id)

    async def transition_incident(
        self,
        session_id: str,
        next_state: IncidentState,
        *,
        event_type: str,
        payload: dict[str, object] | None = None,
    ) -> IncidentSession:
        current_session = incident_session_store.get_session(session_id)
        event_payload = dict(payload or {})
        if next_state == IncidentState.RESOLVED:
            package = build_resolution_package(current_session)
            if package is not None:
                event_payload["resolution_package"] = package.model_dump(mode="json")

        session = incident_session_store.transition_session(
            session_id,
            next_state,
            event_type=event_type,
            payload=event_payload,
        )
        if next_state == IncidentState.RESOLVED:
            persist_resolution_package(session)
            metrics_collector.record_incident_resolved()
        if next_state == IncidentState.FAILED:
            metrics_collector.record_incident_failed()
        await incident_websocket_manager.broadcast_session(session)
        return session

    async def analyze_incident(
        self,
        session_id: str,
        payload: AnalyzeIncidentRequest,
    ) -> AnalyzeIncidentResponse:
        session = self.get_incident(session_id)
        start_time = time.monotonic()

        if session.state == IncidentState.CREATED:
            session = await self.transition_incident(
                session_id,
                IncidentState.INGESTING,
                event_type="analysis_ingesting",
                payload={"actor": "backend"},
            )

        if session.state == IncidentState.INGESTING:
            session = await self.transition_incident(
                session_id,
                IncidentState.RETRIEVING,
                event_type="analysis_retrieving",
                payload={"graph_depth": payload.graph_depth},
            )

        if session.state == IncidentState.RETRIEVING:
            session = await self.transition_incident(
                session_id,
                IncidentState.GRAPH_EXPANDING,
                event_type="analysis_graph_expanding",
                payload={"deployment_window": payload.deployment_window},
            )

        if session.state == IncidentState.GRAPH_EXPANDING:
            session = await self.transition_incident(
                session_id,
                IncidentState.GENERATING_PATCH,
                event_type="analysis_generating_patch",
                payload={"token_budget": payload.token_budget},
            )

        analysis = await run_incident_workflow(
            session.fingerprint,
            ContextBounds(
                graph_depth=payload.graph_depth,
                token_budget=payload.token_budget,
                deployment_window=payload.deployment_window,
                confidence_threshold=payload.confidence_threshold,
            ),
        )

        log_analysis_complete(
            session,
            analysis.root_cause,
            analysis.patch,
            [score.model_dump() for score in analysis.confidence],
        )

        session = await self.transition_incident(
            session_id,
            IncidentState.VALIDATING,
            event_type="analysis_ready",
            payload={
                "analysis": analysis.model_dump(mode="json"),
                "primary_symbol": analysis.context.get("primary_symbol"),
                "indexed_symbols_used": [
                    symbol.get("symbol_id")
                    for symbol in analysis.context.get("indexed_symbols", [])[:5]
                    if isinstance(symbol, dict) and symbol.get("symbol_id")
                ],
            },
        )

        latency = time.monotonic() - start_time
        metrics_collector.record_analysis(latency)

        return AnalyzeIncidentResponse(session=session, analysis=analysis)

    def post_chat_message(self, session_id: str, role: str, content: str) -> IncidentSession:
        session = self.get_incident(session_id)
        log_chat_message(session, role, content)
        return self.get_incident(session_id)

    def log_action(self, session_id: str, action: str, details: dict) -> IncidentSession:
        session = self.get_incident(session_id)
        log_user_action(session, action, details)
        return self.get_incident(session_id)


incident_service = IncidentService()
