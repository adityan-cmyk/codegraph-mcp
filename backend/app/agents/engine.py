from typing import Any

from app.agents.client import hosted_model_client
from app.schemas.incident import IncidentAnalysis


async def generate_resolution(context: dict[str, Any]) -> IncidentAnalysis:
    return await hosted_model_client.analyze(context)