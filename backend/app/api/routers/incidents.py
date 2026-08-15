from fastapi import APIRouter, HTTPException, status

from app.core.incident_service import incident_service
from app.core.incident_store import SessionNotFoundError
from app.schemas.incident import (
    AnalyzeIncidentRequest,
    AnalyzeIncidentResponse,
    ChatMessageRequest,
    CreateIncidentRequest,
    IncidentSession,
    UpdateIncidentStateRequest,
)


router = APIRouter(prefix="/api/incidents", tags=["incidents"])


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_incident(payload: CreateIncidentRequest) -> IncidentSession:
    return await incident_service.create_incident(payload)


@router.get("/", response_model=list[IncidentSession])
def list_incidents() -> list[IncidentSession]:
    return incident_service.list_incidents()


@router.get("/{session_id}")
def get_incident(session_id: str) -> IncidentSession:
    try:
        return incident_service.get_incident(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident session not found.") from exc


@router.post("/{session_id}/state")
async def update_incident_state(session_id: str, payload: UpdateIncidentStateRequest) -> IncidentSession:
    try:
        return await incident_service.transition_incident(
            session_id,
            payload.next_state,
            event_type=payload.event_type,
            payload=payload.payload,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident session not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{session_id}/analyze", response_model=AnalyzeIncidentResponse)
async def analyze_incident(session_id: str, payload: AnalyzeIncidentRequest) -> AnalyzeIncidentResponse:
    try:
        return await incident_service.analyze_incident(session_id, payload)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident session not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{session_id}/chat", response_model=IncidentSession)
def post_chat_message(session_id: str, payload: ChatMessageRequest) -> IncidentSession:
    try:
        return incident_service.post_chat_message(session_id, payload.role, payload.content)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident session not found.") from exc