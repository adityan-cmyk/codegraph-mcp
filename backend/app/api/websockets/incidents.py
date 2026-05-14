from fastapi import WebSocket


async def incident_room_socket(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    await websocket.send_json({"type": "session_open", "session_id": session_id})
    await websocket.close()