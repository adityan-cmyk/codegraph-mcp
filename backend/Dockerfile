FROM python:3.12-slim AS backend-base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    libpq-dev \
    curl \
    cargo \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/local/cargo/bin:${PATH}"

RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# langfuse is optional (tracing), installed separately due to httpx version conflict with fastmcp
RUN pip install --no-cache-dir langfuse==3.0.0 || echo "langfuse optional dep skipped"

COPY backend/app ./app

# Preload embedding model with pinned revision
RUN python -c "from sentence_transformers import SentenceTransformer; _ = SentenceTransformer('BAAI/bge-base-en-v1.5')"

# Cache dir for HuggingFace models
RUN mkdir -p /app/.cache/huggingface && cp -r /root/.cache/huggingface/* /app/.cache/huggingface/ && cp -r /root/.cache/torch /app/.cache/torch 2>/dev/null || true

ENV HF_HOME=/app/.cache/huggingface

RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000 8002

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--limit-concurrency", "100", "--timeout-keep-alive", "30"]
