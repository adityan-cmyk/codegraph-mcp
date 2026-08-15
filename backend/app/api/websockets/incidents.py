from fastapi import WebSocket, WebSocketDisconnect

from app.api.websockets.manager import incident_websocket_manager
from app.core.config import settings
from app.core.incident_store import SessionNotFoundError, incident_session_store


def is_allowed_websocket_origin(origin: str | None) -> bool:
    if origin is None:
        return False
    return origin in settings.cors_allowed_origins


async def incident_room_socket(websocket: WebSocket, session_id: str) -> None:
    if not is_allowed_websocket_origin(websocket.headers.get("origin")):
        await websocket.accept()
        await websocket.send_json({"type": "error", "detail": "WebSocket origin is not allowed."})
        await websocket.close(code=1008)
        return

    try:
        session = incident_session_store.get_session(session_id)
    except SessionNotFoundError:
        await websocket.accept()
        await websocket.send_json({"type": "error", "detail": "Incident session not found."})
        await websocket.close(code=1008)
        return

    await incident_websocket_manager.connect(session_id, websocket)
    await websocket.send_json({"type": "session_snapshot", "session": session.model_dump(mode="json")})

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        incident_websocket_manager.disconnect(session_id, websocket)