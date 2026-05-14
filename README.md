# On-call Assistant

## Infrastructure Stack

- Codebase: Rust monorepo
- Version control: Bitbucket, including source code and `system_prompt.yaml`
- Frontend: ReactJS with Tailwind and shadcn, plus WebSockets
- Backend: Python with FastAPI, Celery, and Redis for task queue and pub/sub
- Data layer:
  - PostgreSQL for event sourcing timeline, session state machine, and eval data
  - Weaviate for vector storage and structured semantic caching
  - Neo4j for structural code graph, dependency relationships, and temporal nodes
- LLM engine: locally hosted GLM-4.7 or Kimi K2 with `nomic-embed-text`
- Agent sandbox: FastMCP with pre-warmed Firecracker microVMs

## Core Architecture Standards

### Explicit Session State Machine

Every incident room operates strictly through these states, tracked in PostgreSQL and broadcasted via WebSockets:

`CREATED -> INGESTING -> RETRIEVING -> GRAPH_EXPANDING -> GENERATING_PATCH -> VALIDATING -> RESOLVED`

Failure transitions should resolve to `FAILED`.

### Strict Internal Schemas

No loose JSON blobs. Everything moving through FastAPI should be strictly typed with Pydantic models such as `IncidentFingerprint` and `ConfidenceScore`.

### Confidence UX

Every LLM response should expose a calibrated confidence badge, for example:

- Retrieval: 0.91
- Graph: 0.88
- Sandbox: Passed

### Context Assembler Service

Context assembly is the core intelligence layer. Avoid naive `retrieve_chunks()` helpers. All LLM context should be compiled through a dedicated module like:

```python
assemble_incident_context(
    fingerprint,
    graph_depth,
    token_budget,
    deployment_window,
    confidence_threshold,
)
```

This enforces strict token budgeting and hierarchical pruning before inference.

## Frontend UI Dashboard

The React frontend is designed as a three-pane, IDE-style layout for Sev-1 outages. FastAPI drives the UI via a single WebSocket connection, routing JSON payloads by type such as `chat_stream`, `telemetry_update`, and `tool_execution`.

### Pane 1: Left Sidebar

- Environment block with inputs for build ID or commit hash and environment (`UAT` or `Prod`)
- Log input area for raw Coralogix or Prometheus stack traces
- `Sync KB` button to trigger background knowledge base updates
- Once `Start Debugging` is clicked, these inputs lock to create an immutable context boundary

### Pane 2: Center Console

- Multi-player chat for collaborating engineers
- Streaming AI output for verified root causes and Rust patches
- `Resolve` action to trigger the continuous learning loop and save the fix

### Pane 3: Right Sidebar

- Thought stream sourced from Langfuse or LangSmith, for example graph traversal and patch generation stages
- MCP terminal showing raw stdout and stderr from the Firecracker sandbox

## Execution Pipeline Phases

### Phase 1: Hybrid Knowledge Base Foundation

Goal: ingest the Rust monorepo deterministically while separating semantic meaning from structural relationships.

- Use `tree-sitter-rust` to parse the monorepo
- Chunk strictly on structural boundaries such as `impl`, `struct`, and `fn`
- Generate deterministic symbol IDs such as `auth::handlers::login_user`
- Store structure in Neo4j with edges like `CALLS`, `IMPLEMENTS`, and `DEPENDS_ON`
- Store embedded Rust chunks in Weaviate, tagged with symbol IDs

### Phase 2: Decoupled Async Pre-Warming and Impact Analysis

Goal: update the knowledge base safely and asynchronously when code is handed off for deployment.

- UI collects `old_commit` and `new_commit`
- FastAPI returns `202 Accepted` and submits a Celery task via Redis
- Worker fetches the Bitbucket diff and extracts modified symbol IDs
- Neo4j is used to compute blast radius across upstream callers and downstream implementors
- Modified and impacted code is re-embedded and upserted into Weaviate and Neo4j
- FastAPI broadcasts a `sync_complete` WebSocket event back to the frontend

### Phase 3: UAT Chaos Engineering

Goal: bootstrap system memory and validate safety before production launch.

- Inject resolved UAT bugs, panic logs, and fix commits into Weaviate as the `ResolvedErrors` baseline
- Intentionally break the UAT environment and inspect how the agent traverses the graph
- Save 20 perfectly diagnosed UAT incidents to PostgreSQL as a golden eval suite
- Re-run the suite on prompt or retrieval changes to detect regression

### Phase 4: Incident Resolution

Goal: fast, deterministic, securely sandboxed debugging.

- Extract an error fingerprint from logs using service, panic type, top frame, and commit hash
- If Weaviate finds a `> 0.95` match in `ResolvedErrors`, return the historical fix immediately
- Otherwise call the dedicated context assembler to dynamically query Neo4j, recent deploy windows, and ranked chunks
- LLM uses the assembled context only; it should not fetch logs itself
- Verification happens via `run_cargo_test` in a pre-warmed Firecracker microVM with a read-only repo mount
- Terminal output is streamed live to the frontend
- The agent returns verified root cause, Rust patch, and confidence scores to the UI

### Phase 5: Continuous Learning and Event Sourcing

Goal: preserve and operationalize every outage resolution.

- Persist every action, tool call, state change, and chat message to PostgreSQL as an immutable event timeline
- On resolution, package the original fingerprint, verified root cause, and applied patch
- Embed and upsert that package into Weaviate's `ResolvedErrors` collection

## Recommended Build Order

### V1

Build only:

- Backend: FastAPI, WebSocket streaming, one incident endpoint
- Retrieval: `tree-sitter-rust`, symbol IDs, Weaviate first, basic Neo4j graph
- UI: log paste, streamed response, tool output pane
- Agent: one MCP tool, `run_cargo_test`
- Sandbox: basic Firecracker execution
- Learning: save resolved incident to Postgres

### V1.1

- Blast radius traversal in Neo4j
- Semantic cache implementation
- Replay timeline via PostgreSQL

### V1.2

- Multi-user session rooms
- Deployment correlation with temporal nodes
- Historical incident reranking
- Dedicated context assembler with token budgeting and dynamic graph depth

### V1.3

- Eval harness integration
- Chaos testing
- Prompt versioning through Bitbucket

### V2

- Advanced ranking
- Cross-encoder rerankers to prevent context explosion
- Autonomous patch validation loops
- Ownership intelligence mapping

## Planned Folder Structure

```text
Heimdall/
├── frontend/
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── components/
│   │   │   ├── layout/
│   │   │   ├── left-sidebar/
│   │   │   ├── center-console/
│   │   │   ├── right-sidebar/
│   │   │   └── shared/
│   │   ├── store/
│   │   ├── hooks/
│   │   ├── services/
│   │   ├── types/
│   │   └── pages/
│   ├── tailwind.config.js
│   ├── tsconfig.json
│   ├── package.json
│   └── vite.config.ts
└── backend/
    ├── app/
    │   ├── main.py
    │   ├── api/
    │   │   ├── routers/
    │   │   └── websockets/
    │   ├── core/
    │   │   ├── config.py
    │   │   ├── state_machine.py
    │   │   └── database/
    │   ├── schemas/
    │   ├── rag/
    │   │   ├── assembler/
    │   │   ├── ingestion/
    │   │   ├── retrieval/
    │   │   └── ranking/
    │   ├── agents/
    │   │   └── prompts/
    │   ├── mcp/
    │   │   ├── tools/
    │   │   └── sandbox/
    │   └── tasks/
    │       └── workers/
    ├── tests/
    │   ├── eval_harness/
    │   ├── unit/
    │   └── integration/
    ├── requirements.txt
    └── Dockerfile
```