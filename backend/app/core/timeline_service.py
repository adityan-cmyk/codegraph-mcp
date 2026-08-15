from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.core.incident_store import incident_session_store
from app.schemas.incident import IncidentEvent, IncidentSession


def append_timeline_event(
    session: IncidentSession,
    event_type: str,
    payload: dict[str, Any],
) -> IncidentEvent:
    """Append an immutable event to the incident timeline without changing state."""
    event = IncidentEvent(
        event_id=uuid4().hex,
        session_id=session.session_id,
        event_type=event_type,
        state=session.state,
        payload=payload,
        created_at=datetime.now(UTC).isoformat(),
    )
    incident_session_store.append_event(session.session_id, event)
    return event


def log_chat_message(session: IncidentSession, role: str, content: str) -> IncidentEvent:
    return append_timeline_event(
        session,
        "chat_message",
        {"role": role, "content": content},
    )


def log_tool_call(
    session: IncidentSession,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> IncidentEvent:
    return append_timeline_event(
        session,
        "tool_call",
        {"tool_name": tool_name, "arguments": arguments, "result": result},
    )


def log_analysis_complete(
    session: IncidentSession,
    root_cause: str,
    patch: str,
    confidence: list[dict[str, str]],
) -> IncidentEvent:
    return append_timeline_event(
        session,
        "analysis_complete",
        {"root_cause": root_cause, "patch": patch, "confidence": confidence},
    )


def log_user_action(
    session: IncidentSession,
    action: str,
    details: dict[str, Any],
) -> IncidentEvent:
    return append_timeline_event(
        session,
        "user_action",
        {"action": action, "details": details},
    )
