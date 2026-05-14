from fastapi import APIRouter

from app.schemas.incident import IncidentFingerprint


router = APIRouter(prefix="/api/incidents", tags=["incidents"])


@router.post("/")
def create_incident(payload: IncidentFingerprint) -> dict[str, str]:
    return {"message": f"Incident received for service {payload.service}"}