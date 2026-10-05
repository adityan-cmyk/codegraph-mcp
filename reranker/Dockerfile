# Cross-encoder reranker sidecar.
#
# Reorders semantic_search results by query-document relevance. Runs on CPU
# behind a strict caller-side timeout — the backend falls back to hybrid
# ordering if this service is slow or down.
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    sentence-transformers==3.0.1 \
    fastapi==0.115.0 \
    uvicorn==0.30.6

# Model is configurable: bge-reranker-base (best quality, ~1GB, slower) or
# cross-encoder/ms-marco-MiniLM-L-6-v2 (fast, ~90MB). CPU box — pick per
# latency budget.
ENV RERANKER_MODEL=BAAI/bge-reranker-base
ENV HF_HOME=/models

COPY server.py .

EXPOSE 8001

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
