from fastapi import APIRouter, HTTPException, status

from app.core.config import settings
from app.core.repositories.chat_repository import ChatRepository
from app.schemas.chat import (
    ChatMessageResponse,
    ChatThreadResponse,
    CreateThreadRequest,
    RenameThreadRequest,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _get_repo() -> ChatRepository:
    return ChatRepository(settings.postgres_dsn)


@router.post("/threads", response_model=ChatThreadResponse)
def create_thread(payload: CreateThreadRequest | None = None) -> ChatThreadResponse:
    payload = payload or CreateThreadRequest()
    repo = _get_repo()
    thread = repo.create_thread(title=payload.title)
    return ChatThreadResponse(**thread)


@router.get("/threads", response_model=list[ChatThreadResponse])
def list_threads() -> list[ChatThreadResponse]:
    repo = _get_repo()
    threads = repo.list_threads()
    return [ChatThreadResponse(**t) for t in threads]


@router.get("/threads/{thread_id}", response_model=ChatThreadResponse)
def get_thread(thread_id: str) -> ChatThreadResponse:
    repo = _get_repo()
    thread = repo.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    return ChatThreadResponse(**thread)


@router.patch("/threads/{thread_id}", response_model=ChatThreadResponse)
def rename_thread(thread_id: str, payload: RenameThreadRequest) -> ChatThreadResponse:
    repo = _get_repo()
    thread = repo.rename_thread(thread_id, payload.title)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    return ChatThreadResponse(**thread)


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_thread(thread_id: str) -> None:
    repo = _get_repo()
    if not repo.delete_thread(thread_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")


@router.get("/threads/{thread_id}/messages", response_model=list[ChatMessageResponse])
def get_thread_messages(thread_id: str) -> list[ChatMessageResponse]:
    repo = _get_repo()
    thread = repo.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    messages = repo.get_messages(thread_id)
    return [ChatMessageResponse(**m) for m in messages]
