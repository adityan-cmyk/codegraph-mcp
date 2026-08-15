from fastapi import APIRouter, HTTPException, status
from app.schemas.kb_sync import KbSyncAcceptedResponse, KbSyncRequest
from app.tasks.workers.kb_sync import enqueue_kb_sync


router = APIRouter(prefix="/api/kb", tags=["kb-sync"])


@router.post("/sync", response_model=KbSyncAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
def queue_kb_sync(payload: KbSyncRequest) -> KbSyncAcceptedResponse:
    try:
        return enqueue_kb_sync(payload.old_commit, payload.new_commit, payload.repository_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc