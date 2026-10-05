"""Cross-encoder rerank service — /rerank reorders docs by query relevance."""

import logging
import os
import time

from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reranker")

app = FastAPI()

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import CrossEncoder

        model_name = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-base")
        started = time.time()
        logger.info("loading cross-encoder: %s", model_name)
        _model = CrossEncoder(model_name, max_length=512)
        logger.info("model loaded in %.1fs", time.time() - started)
    return _model


class RerankRequest(BaseModel):
    query: str
    documents: list[str]


@app.on_event("startup")
def warm() -> None:
    """Load the model before serving — a cold /rerank would exceed the
    backend's 3.5s budget on its first call after every restart."""
    try:
        _get_model()
        logger.info("model preloaded")
    except Exception:
        logger.exception("model preload failed — will retry on first request")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": os.environ.get("RERANKER_MODEL", "?"), "loaded": _model is not None}


@app.post("/rerank")
def rerank(req: RerankRequest) -> dict:
    if not req.query or not req.documents:
        return {"scores": [], "elapsed_ms": 0}
    model = _get_model()
    started = time.time()
    pairs = [(req.query, doc[:2000]) for doc in req.documents[:20]]
    scores = model.predict(pairs).tolist()
    elapsed = (time.time() - started) * 1000
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    return {
        "scores": [round(s, 4) for s in scores],
        "reranked_order": order,
        "elapsed_ms": round(elapsed, 1),
        "model": os.environ.get("RERANKER_MODEL", "?"),
    }
