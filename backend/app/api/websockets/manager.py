from collections import defaultdict
from fastapi import WebSocket
from app.schemas.incident import IncidentSession


class IncidentWebSocketManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[session_id].add(websocket)

    def disconnect(self, session_id: str, websocket: WebSocket) -> None:
        connections = self._connections.get(session_id)
        if not connections:
            return

        connections.discard(websocket)
        if not connections:
            self._connections.pop(session_id, None)

    async def broadcast_session(self, session: IncidentSession) -> None:
        payload = {
            "type": "session_snapshot",
            "session": session.model_dump(mode="json"),
        }
        await self.broadcast_event(payload)

    async def broadcast_event(self, payload: dict[str, object]) -> None:
        stale_connections: list[WebSocket] = []

        for session_id, connections in list(self._connections.items()):
            for connection in set(connections):
                try:
                    await connection.send_json(payload)
                except RuntimeError:
                    stale_connections.append(connection)
                    self.disconnect(session_id, connection)

        for connection in stale_connections:
            for session_id in list(self._connections):
                self.disconnect(session_id, connection)


incident_websocket_manager = IncidentWebSocketManager()