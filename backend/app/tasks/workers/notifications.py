import asyncio
from typing import Any

from app.api.websockets.manager import incident_websocket_manager


def publish_notification(message_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    event = {"type": message_type, **payload}
    asyncio.run(incident_websocket_manager.broadcast_event(event))
    return event