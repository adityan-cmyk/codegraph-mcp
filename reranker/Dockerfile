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

# Model is configurable via RERANKER_MODEL. Default ms-marco-MiniLM-L-6-v2:
# ~100ms/doc on CPU. BAAI/bge-reranker-base is higher quality but ~1s/doc
# on this box — only viable with fewer, shorter docs.
ENV RERANKER_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2
ENV HF_HOME=/models

COPY server.py .

EXPOSE 8001

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
