# Handoff Notes (May 18, 2026)

## 1. Current project state

Base product flow is working end-to-end in local mode:

- Frontend is now a single-screen command center with three major sections and wired actions.
- Chat history supports right-click context actions: edit chat name, new chat, delete chat.
- AI Analysis chat works against LiteLLM through backend endpoint `/api/model/chat`.
- Assistant typing indicator (three dots) is visible while model response is pending.
- Backend and frontend both build/run in current repo state.
- Current local setup has been using memory backends for semantic and graph to avoid infra blockers.

## 2. Move to larger-storage Desktop machine/folder

Use this order after moving/copying repo:

```bash
cd /path/to/oncall
python3 -m venv venv
source venv/bin/activate

cd backend
pip install -r requirements.txt

cd ../frontend
npm install
```

Then run:

```bash
# terminal 1
cd backend
source ../venv/bin/activate
uvicorn app.main:app --reload --port 8000

# terminal 2
cd frontend
npm run dev
```

Important env checks after moving:

- Update `CODEBASE_ROOT_PATH` to the new absolute path.
- Keep `LITELLM_BASE_URL`, `LITELLM_API_KEY`, and `LITELLM_MODEL` valid.
- Confirm backend reads root `.env` (project root, not backend-only `.env`).

## 3. Docker setup for all DB/services

Infra files are now committed in the repo:

- `docker-compose.db.yml`
- `scripts/up-infra.sh`
- `scripts/down-infra.sh`
- `scripts/status-infra.sh`

`docker-compose.db.yml` contains:

```yaml
services:
  postgres:
    image: postgres:16
    container_name: oncall-postgres
    ports:
      - "5432:5432"
    environment:
      POSTGRES_USER: oncall
      POSTGRES_PASSWORD: oncall
      POSTGRES_DB: oncall
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7
    container_name: oncall-redis
    ports:
      - "6379:6379"

  neo4j:
    image: neo4j:5
    container_name: oncall-neo4j
    ports:
      - "7474:7474"
      - "7687:7687"
    environment:
      NEO4J_AUTH: neo4j/password
    volumes:
      - neo4j_data:/data

  t2v-transformers:
    image: semitechnologies/transformers-inference:sentence-transformers-all-MiniLM-L6-v2
    container_name: oncall-t2v-transformers
    ports:
      - "8081:8080"

  weaviate:
    image: semitechnologies/weaviate:latest
    container_name: oncall-weaviate
    depends_on:
      - t2v-transformers
    ports:
      - "8080:8080"
    environment:
      QUERY_DEFAULTS_LIMIT: 25
      AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED: "true"
      PERSISTENCE_DATA_PATH: /var/lib/weaviate
      DEFAULT_VECTORIZER_MODULE: text2vec-transformers
      ENABLE_MODULES: text2vec-transformers
      TRANSFORMERS_INFERENCE_API: http://t2v-transformers:8080
    volumes:
      - weaviate_data:/var/lib/weaviate

volumes:
  postgres_data:
  neo4j_data:
  weaviate_data:
```

Start infra:

```bash
./scripts/up-infra.sh
```

Verify infra:

```bash
./scripts/status-infra.sh
curl -sS http://localhost:8080/v1/.well-known/ready
```

Stop infra:

```bash
./scripts/down-infra.sh
```

Use DB-backed env values in root `.env`:

```bash
INCIDENT_STORE_BACKEND=postgres
INDEX_METADATA_BACKEND=postgres
RESOLVED_ERROR_BACKEND=postgres
EVAL_CASE_BACKEND=postgres
SEMANTIC_INDEX_BACKEND=weaviate
GRAPH_INDEX_BACKEND=neo4j

POSTGRES_DSN=postgresql://oncall:oncall@localhost:5432/oncall
REDIS_URL=redis://localhost:6379/0
CELERY_TASK_ALWAYS_EAGER=false

WEAVIATE_URL=http://localhost:8080
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password
```

Run backend + worker with infra:

```bash
# terminal 1
cd backend
source ../venv/bin/activate
uvicorn app.main:app --reload --port 8000

# terminal 2
cd backend
source ../venv/bin/activate
celery -A app.tasks.celery_app.celery_app worker --loglevel=info
```

## 4. What is left to finish

1. Commit and standardize infrastructure files:
  - Keep the compose and helper scripts in sync when adding/changing infra services.

2. Run full persistence validation in DB mode:
   - Restart backend and verify incidents, timelines, and eval cases persist.
   - Validate index snapshot replay path against Postgres metadata.

3. Verify async KB sync production path:
   - Confirm Redis + Celery worker processing, task status updates, and frontend feedback.

4. Re-enable and validate real graph + semantic flows:
   - Verify Weaviate query quality and Neo4j traversal on real repo indexing.

5. Firecracker hardening:
   - Move from stub sandbox to real Firecracker runtime in target environment.
   - Validate kernel/rootfs setup and isolation boundaries.

6. Test coverage and CI:
   - Add frontend tests for chat history context menu + rename flow.
   - Add integration tests for `/api/model/chat` and incident lifecycle with non-memory backends.

7. Python 3.14 dependency hygiene:
   - Replace local FastMCP site-package patch with a clean pinned/fixed version strategy.

## 5. Quick smoke test checklist after move

```bash
curl -sS http://127.0.0.1:8000/api/health
curl -sS http://127.0.0.1:8000/api/model/status
curl -sS -X POST http://127.0.0.1:8000/api/model/chat -H 'Content-Type: application/json' -d '{"message":"hello"}'
```

Frontend checks:

- Open http://localhost:5173
- Create new incident
- Run Start Analysis
- Send chat message and confirm typing indicator appears
- Right-click chat history item and confirm edit/new/delete all work
