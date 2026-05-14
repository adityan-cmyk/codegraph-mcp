from pydantic import BaseModel


class ContextBounds(BaseModel):
    graph_depth: int = 2
    token_budget: int = 4096
    deployment_window: str = "24h"
    confidence_threshold: float = 0.8


class ContextChunk(BaseModel):
    symbol_id: str
    score: float
    content: str